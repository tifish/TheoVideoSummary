"""用真实的 Chrome（专用、已登录小号的配置）获取字幕。

做法与真人看视频一致：打开视频页 → 通过播放器开启英文字幕 → 截获播放器自己
发出的 timedtext 请求（带登录 cookies 与 PO Token）。不伪造任何请求。

Chrome 以普通方式启动（不带 Playwright 的自动化启动参数，navigator.webdriver 为 false），
脚本只通过本地调试端口连接。由 Playwright 直接启动的 Chrome 会被 YouTube 识别，
字幕请求返回空内容。

首次使用先登录：python fetch.py --login
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import urllib.request
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlparse, urlunparse

ROOT = Path(__file__).resolve().parent
PROFILE_DIR = ROOT / ".browser-profile"
# 本机 Edge 有 UserDataDir 组策略，无法启动独立配置，所以用 Chrome
CAPTION_TIMEOUT = 90  # 秒；等待播放器发出字幕请求（含片头广告）

# 在页面中执行：静音播放、跳过广告、开启英文字幕轨
_DRIVE_PLAYER_JS = """(lang) => {
  const p = document.querySelector('#movie_player');
  if (!p || !p.getOption) return 'noplayer';
  p.mute(); p.playVideo();
  if (p.classList.contains('ad-showing')) {
    document.querySelector('.ytp-skip-ad-button, .ytp-ad-skip-button-modern')?.click();
    return 'ad';
  }
  p.loadModule('captions');
  const tl = p.getOption('captions', 'tracklist') || [];
  const t = tl.find(t => t.languageCode === lang) || tl.find(t => (t.languageCode || '').startsWith('en'));
  if (t) p.setOption('captions', 'track', t);
  return 'captions';
}"""

_PLAYER_STATE_JS = """() => {
  const pr = window.ytInitialPlayerResponse || {};
  const tracks = pr.captions?.playerCaptionsTracklistRenderer?.captionTracks || [];
  const vd = pr.videoDetails || {};
  const mf = pr.microformat?.playerMicroformatRenderer || {};
  return {
    status: pr.playabilityStatus?.status || null,
    reason: pr.playabilityStatus?.reason || '',
    tracks: tracks.map(t => ({lang: t.languageCode, kind: t.kind || ''})),
    title: vd.title || '',
    duration: Number(vd.lengthSeconds || 0),
    description: vd.shortDescription || '',
    published: mf.publishDate || mf.uploadDate || '',
    is_upcoming: !!vd.isUpcoming,
    is_live: !!mf.liveBroadcastDetails?.isLiveNow,
  };
}"""


def is_logged_in(context) -> bool:
    return any(c["name"] in ("LOGIN_INFO", "SAPISID") for c in context.cookies("https://www.youtube.com"))


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class _Chrome:
    """以普通方式启动 Chrome 打开专用配置，并通过本地调试端口连接。"""

    def __init__(self, pw) -> None:
        port = _free_port()
        self.proc = subprocess.Popen([
            _chrome_exe(), f"--user-data-dir={PROFILE_DIR}", f"--remote-debugging-port={port}",
            "--no-first-run", "--no-default-browser-check", "--mute-audio", "about:blank",
        ])
        endpoint = f"http://127.0.0.1:{port}"
        for _ in range(60):
            if self.proc.poll() is not None:
                raise RuntimeError("Chrome exited at startup (is the .browser-profile window already open?)")
            try:
                urllib.request.urlopen(endpoint + "/json/version", timeout=1)
                break
            except OSError:
                time.sleep(0.5)
        else:
            self.proc.kill()
            raise RuntimeError("Chrome debugging port did not come up")
        self.browser = pw.chromium.connect_over_cdp(endpoint)
        self.context = self.browser.contexts[0]

    def close(self) -> None:
        try:
            self.browser.new_browser_cdp_session().send("Browser.close")
        except Exception:  # noqa: BLE001
            pass
        try:
            self.proc.wait(timeout=20)
        except subprocess.TimeoutExpired:
            self.proc.kill()


def _chrome_exe() -> str:
    for base in (os.environ.get("PROGRAMFILES"), os.environ.get("PROGRAMFILES(X86)"), os.environ.get("LOCALAPPDATA")):
        if base and (p := Path(base) / "Google/Chrome/Application/chrome.exe").exists():
            return str(p)
    raise FileNotFoundError("Google Chrome not found")


def login() -> None:
    """以普通方式（非自动化）启动 Chrome 打开专用配置，让用户手动登录 YouTube。

    Google 会拒绝在自动化控制的浏览器中登录，所以登录这一步不经过 Playwright；
    之后抓字幕时由 Playwright 复用该配置中保存的登录状态。
    """
    print("请在弹出的 Chrome 窗口中登录 YouTube 小号，登录完成后关闭整个浏览器窗口。", flush=True)
    subprocess.run([
        _chrome_exe(), f"--user-data-dir={PROFILE_DIR}", "--no-first-run", "--no-default-browser-check",
        "https://accounts.google.com/ServiceLogin?service=youtube&continue=https://www.youtube.com/",
    ])
    time.sleep(2)  # 等 Chrome 写完 cookies

    from playwright.sync_api import sync_playwright

    with sync_playwright() as pw:
        chrome = _Chrome(pw)
        logged = is_logged_in(chrome.context)
        chrome.close()
    print("登录状态：" + ("已登录 ✓" if logged else "未检测到登录，请重新运行 python fetch.py --login"))


def _pick_lang(tracks: list[dict]) -> str | None:
    en = [t for t in tracks if (t["lang"] or "").startswith("en")]
    manual = [t for t in en if t["kind"] != "asr"]
    return (manual or en or [{"lang": None}])[0]["lang"]


class BrowserSession:
    """整轮运行共用一个浏览器；首次需要时才启动窗口。"""

    def __init__(self) -> None:
        self._pw = None
        self._chrome = None
        self._page = self._page_vid = None
        self._hits: list = []

    @staticmethod
    def available() -> bool:
        if not PROFILE_DIR.exists():
            return False
        try:
            import playwright  # noqa: F401
        except ImportError:
            return False
        return True

    def _context(self):
        if self._chrome is None:
            from playwright.sync_api import sync_playwright

            self._pw = self._pw or sync_playwright().start()
            self._chrome = _Chrome(self._pw)
            if not is_logged_in(self._chrome.context):
                raise RuntimeError("browser profile is not logged in; run: python fetch.py --login")
        return self._chrome.context

    def close(self) -> None:
        self._close_page()
        if self._chrome is not None:
            self._chrome.close()
        if self._pw is not None:
            try:
                self._pw.stop()
            except Exception:  # noqa: BLE001
                pass
        self._chrome = self._pw = None

    def _close_page(self) -> None:
        if self._page is not None:
            try:
                self._page.close()
            except Exception:  # noqa: BLE001
                pass
        self._page = self._page_vid = None
        self._hits = []

    def _open(self, video_id: str):
        """打开视频页并读取播放器数据；同一视频的 details() 与 json3() 共用一次页面访问。"""
        if self._page is not None and self._page_vid == video_id:
            return self._page, self._state
        self._close_page()
        page = self._context().new_page()
        hits = self._hits = []
        page.on("response", lambda r: hits.append(r)
                if "/api/timedtext" in r.url and "tlang=" not in r.url else None)
        self._page, self._page_vid = page, video_id
        page.goto(f"https://www.youtube.com/watch?v={video_id}", wait_until="domcontentloaded", timeout=60000)
        page.wait_for_selector("#movie_player", timeout=60000)
        self._state = page.evaluate(_PLAYER_STATE_JS)
        if self._state["status"] == "LOGIN_REQUIRED":
            raise RuntimeError(f"LOGIN_REQUIRED: {self._state['reason']}")
        return page, self._state

    def details(self, video_id: str) -> dict:
        """视频元数据，字段与 yt-dlp 的 extract_info 对齐（fetch.py 使用的部分）。"""
        try:
            _, st = self._open(video_id)
        except Exception:
            self._close_page()
            raise
        ts = None
        if st["published"]:
            ts = datetime.fromisoformat(st["published"].replace("Z", "+00:00"))
        return {
            "title": st["title"],
            "duration": st["duration"],
            "description": st["description"],
            "upload_date": ts.strftime("%Y%m%d") if ts else None,
            "timestamp": int(ts.timestamp()) if ts and ts.tzinfo else None,
            "live_status": "is_upcoming" if st["is_upcoming"] else "is_live" if st["is_live"] else None,
        }

    def json3(self, video_id: str) -> dict:
        """返回字幕的 json3 数据。被 YouTube 拦截时抛出的异常信息包含 LOGIN_REQUIRED、429 或 EMPTY_CAPTIONS。"""
        try:
            page, state = self._open(video_id)
            ctx = page.context
            hits = self._hits
            if state["status"] not in (None, "OK"):
                raise RuntimeError(f"video not playable: {state['status']} {state['reason']}")
            lang = _pick_lang(state["tracks"])
            if not lang:
                raise RuntimeError("no English caption track")

            deadline = time.time() + CAPTION_TIMEOUT
            while not hits and time.time() < deadline:
                page.evaluate(_DRIVE_PLAYER_JS, lang)
                page.wait_for_timeout(1000)
            if not hits:
                raise RuntimeError("player did not request captions in time")

            resp = hits[0]
            if resp.status == 429:
                raise RuntimeError("timedtext HTTP 429 Too Many Requests")
            if resp.status != 200:
                raise RuntimeError(f"timedtext HTTP {resp.status}")
            body = resp.text()
            if not body.strip():
                # 已登录、带 PO Token 仍返回 200 空内容：字幕接口被 YouTube 限制
                raise RuntimeError("EMPTY_CAPTIONS: timedtext returned 200 with empty body (rate-limited)")
            try:
                return json.loads(body)
            except ValueError:
                # 播放器用的不是 json3 格式时，用同一会话（同 cookies / PO Token）改格式再取一次
                u = urlparse(resp.url)
                q = parse_qs(u.query)
                q["fmt"] = ["json3"]
                r = ctx.request.get(urlunparse(u._replace(query=urlencode(q, doseq=True))))
                if r.status != 200:
                    raise RuntimeError(f"timedtext HTTP {r.status}")
                return r.json()
        finally:
            self._close_page()

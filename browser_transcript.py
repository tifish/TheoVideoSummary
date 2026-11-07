"""通过 Playwright 连接日常使用的 Edge，复用默认配置中的 YouTube 登录状态。

做法与真人看视频一致：打开视频页 → 通过播放器开启英文字幕 → 截获播放器自己
发出的 timedtext 请求（带登录 cookies 与 PO Token）。不伪造任何请求。

Edge 以普通方式启动，Playwright 通过本地调试端口连接。
首次连接前须退出未开启调试端口的 Edge；任务结束只关闭任务自己的标签页。

首次使用先登录：python fetch.py --login
"""

from __future__ import annotations

import json
import os
import random
import subprocess
import urllib.request
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlparse, urlunparse

ROOT = Path(__file__).resolve().parent
PROFILE_DIR = Path(os.environ["LOCALAPPDATA"]) / "Microsoft/Edge/User Data"
CAPTION_TIMEOUT = 90  # 秒；等待播放器发出字幕请求（含片头广告）
# 模拟真人观看节奏：打开后先看一会儿再开字幕，拿到字幕后继续播放一段时间再关闭
BEFORE_CAPTIONS = (2, 5)  # 秒
KEEP_WATCHING = (20, 60)  # 秒
STALL_SECONDS = 20       # 播放时间这么久没有前进（如片头广告加载失败）就刷新页面
MAX_RELOADS = 2
# 正片已播放这么久仍没有字幕请求：关掉再打开字幕（最多 MAX_RESTARTS 次），仍不行再刷新页面
CAPTION_GRACE = 15       # 秒
MAX_RESTARTS = 2
# 片头广告播放的时间不计入 CAPTION_TIMEOUT；同一页面广告累计超过这么久就刷新页面
MAX_AD_SECONDS = 240

_VIDEO_STATE_JS = """() => {
  const p = document.querySelector('#movie_player');
  return {t: p?.querySelector('video')?.currentTime ?? -1, ad: !!p?.classList.contains('ad-showing')};
}"""

# 关闭字幕模块；下一次 _DRIVE_PLAYER_JS 会重新加载并选中字幕轨（相当于手动按两次 C）
_RESTART_CAPTIONS_JS = "() => document.querySelector('#movie_player')?.unloadModule?.('captions')"

# 频道「视频」页中的列表（ytInitialData 里的 lockupViewModel）
_CHANNEL_VIDEOS_JS = """() => {
  const out = [];
  const walk = (o) => {
    if (!o || typeof o !== 'object') return;
    const lv = o.lockupViewModel;
    if (lv && lv.contentId) {
      const badges = JSON.stringify(lv.contentImage || {}).match(/"text":"(\\d+(?::\\d+)+)"/);
      out.push({id: lv.contentId,
                title: lv.metadata?.lockupMetadataViewModel?.title?.content || '',
                duration: badges ? badges[1] : null});
      return;
    }
    for (const k in o) walk(o[k]);
  };
  walk(window.ytInitialData);
  return out;
}"""

# 在页面中执行：静音播放、跳过广告、开启英文字幕轨
_DRIVE_PLAYER_JS = """(lang) => {
  const p = document.querySelector('#movie_player');
  if (!p || !p.getOption) return 'noplayer';
  p.mute(); p.playVideo();
  if (p.classList.contains('ad-showing')) {
    document.querySelector('.ytp-skip-ad-button, .ytp-ad-skip-button-modern, .ytp-ad-skip-button, [id^="skip-button"] button')?.click();
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


class _Edge:
    """连接或启动默认 Edge；不复制配置，也不关闭用户的浏览器。"""

    def __init__(self, pw) -> None:
        # Edge 使用随机调试端口时，会在默认数据目录记录连接地址。
        try:
            lines = (PROFILE_DIR / "DevToolsActivePort").read_text().splitlines()
            existing_port = int(lines[0])
            endpoint = f"http://127.0.0.1:{existing_port}"
            with urllib.request.urlopen(endpoint + "/json/version", timeout=1):
                pass
        except (OSError, ValueError, IndexError):
            endpoint = None
        if endpoint:
            self.browser = pw.chromium.connect_over_cdp(endpoint)
            self.context = self.browser.contexts[0]
            return

        self.proc = subprocess.Popen([
            _edge_exe(), "--remote-debugging-port=0",
            "--no-first-run", "--no-default-browser-check", "about:blank",
        ])
        for _ in range(60):
            if self.proc.poll() is not None:
                raise RuntimeError("Edge is already running without remote debugging; fully exit Edge, then rerun python fetch.py")
            try:
                lines = (PROFILE_DIR / "DevToolsActivePort").read_text().splitlines()
                port = int(lines[0])
                endpoint = f"http://127.0.0.1:{port}"
                with urllib.request.urlopen(endpoint + "/json/version", timeout=1):
                    pass
                break
            except (OSError, ValueError, IndexError):
                time.sleep(0.5)
        else:
            raise RuntimeError("Edge debugging port did not come up; fully exit Edge (including background processes) and retry")
        self.browser = pw.chromium.connect_over_cdp(endpoint)
        self.context = self.browser.contexts[0]

def _edge_exe() -> str:
    for base in (os.environ.get("PROGRAMFILES"), os.environ.get("PROGRAMFILES(X86)"), os.environ.get("LOCALAPPDATA")):
        if base and (p := Path(base) / "Microsoft/Edge/Application/msedge.exe").exists():
            return str(p)
    raise FileNotFoundError("Microsoft Edge not found")


def login() -> None:
    """打开日常 Edge，供用户手动登录；抓取时复用该默认配置。"""
    print("请在默认 Edge 中登录 YouTube。登录后完全退出 Edge，再运行 python fetch.py。", flush=True)
    subprocess.Popen([
        _edge_exe(), "--no-first-run", "--no-default-browser-check",
        "https://accounts.google.com/ServiceLogin?service=youtube&continue=https://www.youtube.com/",
    ])


def _pick_lang(tracks: list[dict]) -> str | None:
    en = [t for t in tracks if (t["lang"] or "").startswith("en")]
    manual = [t for t in en if t["kind"] != "asr"]
    return (manual or en or [{"lang": None}])[0]["lang"]


class BrowserSession:
    """整轮运行共用一个浏览器；首次需要时才启动窗口。"""

    def __init__(self) -> None:
        self._pw = None
        self._edge = None
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
        if self._edge is None:
            from playwright.sync_api import sync_playwright

            self._pw = self._pw or sync_playwright().start()
            self._edge = _Edge(self._pw)
            if not is_logged_in(self._edge.context):
                raise RuntimeError("browser profile is not logged in; run: python fetch.py --login")
        return self._edge.context

    def close(self) -> None:
        self._close_page()
        # 停止 Playwright 连接，保留默认 Edge 和用户的其他标签页。
        if self._pw is not None:
            try:
                self._pw.stop()
            except Exception:  # noqa: BLE001
                pass
        self._edge = self._pw = None

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
        seen = self._seen = []
        def on_response(r) -> None:
            # 只收本视频的字幕：片头广告的字幕也走同一个接口；tlang= 是机器翻译
            if "/api/timedtext" in r.url:
                q = parse_qs(urlparse(r.url).query)
                seen.append(q.get("v", ["?"])[0])
                if q.get("v") == [video_id] and "tlang" not in q:
                    hits.append(r)

        page.on("response", on_response)
        self._page, self._page_vid = page, video_id
        page.goto(f"https://www.youtube.com/watch?v={video_id}", wait_until="domcontentloaded", timeout=60000)
        page.wait_for_selector("#movie_player", timeout=60000)
        self._state = page.evaluate(_PLAYER_STATE_JS)
        if self._state["status"] == "LOGIN_REQUIRED":
            raise RuntimeError(f"LOGIN_REQUIRED: {self._state['reason']}")
        return page, self._state

    def list_videos(self, channel_url: str, limit: int) -> list[dict]:
        """频道最新视频列表：[{id, title, duration(秒或 None)}]，按页面顺序（新→旧）。"""
        page = self._context().new_page()
        try:
            page.goto(channel_url, wait_until="domcontentloaded", timeout=60000)
            page.wait_for_function("window.ytInitialData", timeout=60000)
            page.wait_for_timeout(random.uniform(*BEFORE_CAPTIONS) * 1000)
            items = page.evaluate(_CHANNEL_VIDEOS_JS)
        finally:
            page.close()
        if not items:
            raise RuntimeError("no videos found on channel page (layout changed?)")
        seen, out = set(), []
        for it in items:
            if it["id"] in seen:
                continue
            seen.add(it["id"])
            dur = None
            if it["duration"]:
                dur = 0
                for part in it["duration"].split(":"):
                    dur = dur * 60 + int(part)
            out.append({"id": it["id"], "title": it["title"], "duration": dur})
        return out[:limit]

    def details(self, video_id: str) -> dict:
        """视频元数据（fetch.py 登记新视频所需的字段）。"""
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

            page.evaluate("() => { const p = document.querySelector('#movie_player'); p?.mute?.(); p?.playVideo?.(); }")
            page.wait_for_timeout(random.uniform(*BEFORE_CAPTIONS) * 1000)
            deadline = time.time() + CAPTION_TIMEOUT
            reloads = restarts = 0
            last_t, last_move, watched, ad_seconds = None, time.time(), 0, 0.0
            vs = {}

            def reload() -> None:
                nonlocal reloads, restarts, last_t, last_move, watched, ad_seconds, deadline
                reloads += 1
                page.reload(wait_until="domcontentloaded", timeout=60000)
                page.wait_for_selector("#movie_player", timeout=60000)
                page.wait_for_timeout(random.uniform(*BEFORE_CAPTIONS) * 1000)
                restarts, last_t, last_move, watched, ad_seconds = 0, None, time.time(), 0, 0.0
                deadline = max(deadline, time.time() + CAPTION_TIMEOUT / 2)

            while not hits and time.time() < deadline:
                tick = time.time()
                page.evaluate(_DRIVE_PLAYER_JS, lang)
                page.wait_for_timeout(1000)
                vs = page.evaluate(_VIDEO_STATE_JS)
                if vs["ad"]:
                    # 广告时间不计入等待字幕的时限（长广告或连播多条广告）
                    spent = time.time() - tick
                    ad_seconds += spent
                    if ad_seconds <= MAX_AD_SECONDS:
                        deadline += spent
                    elif reloads < MAX_RELOADS:
                        reload()
                        continue
                if vs["t"] != last_t:
                    last_t, last_move = vs["t"], time.time()
                    if not vs["ad"]:
                        watched += 1
                elif time.time() - last_move > STALL_SECONDS and reloads < MAX_RELOADS:
                    # 播放卡住（常见于片头广告加载失败）：像真人一样刷新页面
                    reload()
                    continue
                if watched >= CAPTION_GRACE and not hits:
                    # 正片在播放但播放器没有请求字幕：先关掉再打开字幕，仍不行就刷新页面
                    watched = 0
                    if restarts < MAX_RESTARTS:
                        restarts += 1
                        page.evaluate(_RESTART_CAPTIONS_JS)
                    elif reloads < MAX_RELOADS:
                        reload()
            if not hits:
                raise RuntimeError(
                    f"player did not request captions in time (reloads: {reloads}, restarts: {restarts}, "
                    f"ad: {vs.get('ad')}, ad seconds: {ad_seconds:.0f}, t: {vs.get('t')}, "
                    f"other timedtext: {self._seen})")

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
                data = json.loads(body)
            except ValueError:
                # 播放器用的不是 json3 格式时，用同一会话（同 cookies / PO Token）改格式再取一次
                u = urlparse(resp.url)
                q = parse_qs(u.query)
                q["fmt"] = ["json3"]
                r = ctx.request.get(urlunparse(u._replace(query=urlencode(q, doseq=True))))
                if r.status != 200:
                    raise RuntimeError(f"timedtext HTTP {r.status}")
                data = r.json()
            page.wait_for_timeout(random.uniform(*KEEP_WATCHING) * 1000)
            return data
        finally:
            self._close_page()

"""通过 Browser MCP 复用日常浏览器的 YouTube 登录状态。

仅读取网页和播放器自然发出的字幕响应，不重放或伪造字幕请求。
搜索页先安装静音/捕获逻辑，再通过 YouTube 页面链接进入视频。
MCP 单次输出有长度限制，因此字幕在页面内保存，分段传回 Python。
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import queue
import re
import shutil
import threading
import time
from concurrent.futures import Future
from datetime import datetime
from urllib.parse import quote

MCP_PACKAGE = "@browsermcp/mcp@2.5.0"
CAPTION_TIMEOUT = 90
MAX_AD_SECONDS = 240
STALL_SECONDS = 20
MAX_RELOADS = 2
CAPTION_GRACE = 15
MAX_RESTARTS = 2
CHUNK_SIZE = 8000  # 即使字符被 JSON 转义，单次输出仍低于 MCP 的截断上限。


class _MCP:
    """在同一异步任务中维护 MCP 生命周期，给抓取流程提供同步接口。"""

    def __init__(self) -> None:
        self._requests = queue.Queue()
        self._ready = Future()
        self._closed = False
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        try:
            self._ready.result(timeout=75)
        except Exception:
            self.close()
            raise

    def _run(self) -> None:
        try:
            asyncio.run(self._serve())
        except Exception as exc:
            if not self._ready.done():
                self._ready.set_exception(RuntimeError(f"Browser MCP connection failed: {exc}"))
        finally:
            self._closed = True
            while not self._requests.empty():
                request = self._requests.get_nowait()
                if request is not None and not request[1].done():
                    request[1].set_exception(RuntimeError("Browser MCP connection closed"))

    async def _serve(self) -> None:
        from mcp import ClientSession, StdioServerParameters
        from mcp.client.stdio import stdio_client

        params = StdioServerParameters(command=shutil.which("npx"), args=["--yes", MCP_PACKAGE])
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await asyncio.wait_for(session.initialize(), timeout=60)
                tools = await session.list_tools()
                if "browser" not in {tool.name for tool in tools.tools}:
                    raise RuntimeError("Browser MCP does not expose the browser tool; check its version")
                self._ready.set_result(True)
                while True:
                    request = await asyncio.to_thread(self._requests.get)
                    if request is None:
                        break
                    commands, future = request
                    try:
                        result = await asyncio.wait_for(
                            session.call_tool("browser", {"commands": commands}), timeout=45
                        )
                        output = "\n".join(c.text for c in result.content if c.type == "text")
                        if result.isError:
                            # 不回显页面正文、请求头或带签名的字幕 URL。
                            raise RuntimeError("Browser MCP command failed: " + commands.splitlines()[-1].split(" ")[0])
                        future.set_result(output)
                    except Exception as exc:
                        future.set_exception(exc)

    def call(self, commands: str) -> str:
        if self._closed:
            raise RuntimeError("Browser MCP connection is closed")
        result = Future()
        self._requests.put((commands, result))
        return result.result(timeout=55)

    def close(self) -> None:
        if not self._closed:
            self._requests.put(None)
            self._thread.join(timeout=55)
        # 只退出自己的 stdio 客户端，不关闭共享 daemon 或用户浏览器。


_INSTALL_CAPTURE_JS = r"""(videoId) => {
  const store = window.__theoCaptions = {
    videoId, error: null, serialized: null, eventCount: 0, lastStartMs: null
  };
  const pause = () => {
    document.querySelector('#movie_player')?.pauseVideo?.();
    document.querySelectorAll('video,audio').forEach(v => v.pause());
  };
  const mute = () => document.querySelectorAll('video,audio').forEach(v => {
    v.defaultMuted = true; v.muted = true; v.volume = 0;
  });
  document.addEventListener('play', mute, true);
  document.addEventListener('volumechange', mute, true);
  new MutationObserver(mute).observe(document.documentElement, {childList:true, subtree:true});
  mute();
  const save = (url, status, body) => {
    const u = new URL(url, location.href);
    if (!u.pathname.endsWith('/api/timedtext') || u.searchParams.get('v') !== videoId ||
        u.searchParams.has('tlang') || !(u.searchParams.get('lang') || '').startsWith('en') ||
        store.serialized !== null || store.error) return;
    try {
      if (status === 429) throw new Error('timedtext HTTP 429 Too Many Requests');
      if (status !== 200) throw new Error('timedtext HTTP ' + status);
      if (typeof body === 'string' && !body.trim())
        throw new Error('EMPTY_CAPTIONS: timedtext returned 200 with empty body (rate-limited)');
      let data;
      if (typeof body !== 'string') data = body;
      else if (body.trimStart().startsWith('<')) {
        const xml = new DOMParser().parseFromString(body, 'application/xml');
        if (xml.querySelector('parsererror')) throw new Error('Invalid caption XML');
        data = {events:[...xml.querySelectorAll('p, text')].map(e => ({
          tStartMs:e.tagName === 'text' ? Number(e.getAttribute('start')) * 1000 : Number(e.getAttribute('t')),
          dDurationMs:e.tagName === 'text' ? Number(e.getAttribute('dur')) * 1000 : Number(e.getAttribute('d')),
          segs:[{utf8:e.textContent}]
        }))};
      } else data = JSON.parse(body);
      if (!Array.isArray(data?.events)) throw new Error('Invalid caption events');
      // ASCII 序列化可避免分块切开 UTF-16 代理对；Python 合并后一次性解码。
      store.serialized = JSON.stringify(data).replace(/[\u007f-\uffff]/g,
        c => '\\u' + c.charCodeAt(0).toString(16).padStart(4,'0'));
      store.eventCount = data.events.length;
      store.lastStartMs = data.events.at(-1)?.tStartMs ?? null;
    } catch (e) { store.error = e.message; }
    pause();
  };
  const originalFetch = window.fetch;
  window.fetch = async function(...args) {
    const response = await originalFetch.apply(this, args);
    if (response.url.includes('/api/timedtext'))
      response.clone().text().then(body => save(response.url, response.status, body));
    return response;
  };
  const originalOpen = XMLHttpRequest.prototype.open;
  XMLHttpRequest.prototype.open = function(method, url, ...args) {
    this.addEventListener('load', () => {
      if (!String(url).includes('/api/timedtext')) return;
      try { save(this.responseURL || url, this.status,
        this.responseType === 'json' ? this.response : this.responseText); }
      catch (_) { store.error = 'Could not read player caption response'; pause(); }
    });
    return originalOpen.call(this, method, url, ...args);
  };
  return true;
}"""

_PLAYER_STATE_JS = """(videoId) => {
  const p = document.querySelector('#movie_player');
  const candidates = [p?.getPlayerResponse?.(), window.ytInitialPlayerResponse];
  const pr = candidates.find(r => r?.videoDetails?.videoId === videoId);
  if (!pr) return null;
  const vd = pr.videoDetails || {};
  const mf = pr.microformat?.playerMicroformatRenderer || {};
  return {
    status:pr.playabilityStatus?.status ?? null, reason:pr.playabilityStatus?.reason || '',
    tracks:(pr.captions?.playerCaptionsTracklistRenderer?.captionTracks || [])
      .map(t => ({lang:t.languageCode,kind:t.kind || ''})),
    title:vd.title, duration:Number(vd.lengthSeconds || 0), description:vd.shortDescription || '',
    published:mf.publishDate || mf.uploadDate || '', is_upcoming:!!vd.isUpcoming,
    is_live:!!mf.liveBroadcastDetails?.isLiveNow
  };
}"""

_CHANNEL_VIDEOS_JS = r"""(limit) => {
  const out = [], seen = new Set();
  const add = (id,title,duration) => {
    if (!id || seen.has(id)) return;
    seen.add(id); out.push({id,title,duration});
  };
  const walk = o => {
    if (!o || typeof o !== 'object') return;
    const lv = o.lockupViewModel;
    if (lv?.contentId) {
      const d = JSON.stringify(lv.contentImage || {}).match(/"text":"(\d+(?::\d+)+)"/);
      add(lv.contentId,lv.metadata?.lockupMetadataViewModel?.title?.content || '',d?.[1] || null);
      return;
    }
    for (const key in o) walk(o[key]);
  };
  walk(window.ytInitialData);
  for (const a of document.querySelectorAll('a[href*="/watch?"]')) {
    const id = new URL(a.href).searchParams.get('v');
    const card = a.closest('ytd-rich-item-renderer,ytd-grid-video-renderer');
    if (!card) continue;
    const title = card.querySelector('#video-title')?.textContent?.trim() || a.getAttribute('title') || '';
    const d = card.textContent.match(/\b\d+(?::\d{2}){1,2}\b/);
    add(id,title,d?.[0] || null);
  }
  return out.slice(0,limit);
}"""

_CAPTURE_STATUS_JS = """() => {
  const c = window.__theoCaptions;
  if (!c) throw new Error('Capture disappeared after a full navigation');
  const p = document.querySelector('#movie_player');
  return {error:c.error,length:c.serialized?.length ?? null,eventCount:c.eventCount,
    lastStartMs:c.lastStartMs,ad:!!p?.classList.contains('ad-showing'),
    time:p?.querySelector('video')?.currentTime ?? -1};
}"""

_DRIVE_PLAYER_JS = """(lang) => {
  const p = document.querySelector('#movie_player');
  const c = window.__theoCaptions;
  if (!p || !c || c.serialized !== null || c.error) return {skip:false};
  p.mute(); p.playVideo();
  if (p.classList.contains('ad-showing')) {
    const button = [...p.querySelectorAll('button,[role=button]')].find(b =>
      /^(Skip|Skip ad|跳过|跳过广告)$/i.test((b.innerText || b.getAttribute('aria-label') || '').trim()) &&
      b.getClientRects().length);
    if (button) button.setAttribute('data-theo-caption-skip','true');
    return {skip:!!button};
  }
  p.loadModule('captions');
  const tracks = p.getOption('captions','tracklist') || [];
  const track = tracks.find(t => t.languageCode === lang);
  if (track) p.setOption('captions','track',track);
  const button = p.querySelector('.ytp-subtitles-button');
  if (button?.getAttribute('aria-pressed') !== 'true') button?.click();
  return {skip:false};
}"""


def _pick_lang(tracks: list[dict]) -> str | None:
    english = [t for t in tracks if (t["lang"] or "").startswith("en")]
    manual = [t for t in english if t["kind"] != "asr"]
    return (manual or english or [{"lang": None}])[0]["lang"]


class BrowserSession:
    """每轮共用一个 MCP 客户端；只操作、关闭本实例创建的后台标签页。"""

    def __init__(self) -> None:
        self._mcp = None
        self._page = self._page_vid = None
        self._state = None
        self._tabs = set()

    @staticmethod
    def require_dependencies() -> None:
        if importlib.util.find_spec("mcp") is None:
            raise RuntimeError("Browser MCP Python dependency missing; run .venv\\Scripts\\python.exe -m pip install -r requirements.txt")
        if not shutil.which("npx"):
            raise RuntimeError("Browser MCP requires Node.js/npm (npx not found on PATH)")

    @staticmethod
    def available() -> bool:
        return importlib.util.find_spec("mcp") is not None and shutil.which("npx") is not None

    def _command(self, commands: str) -> str:
        if self._mcp is None:
            self.require_dependencies()
            self._mcp = _MCP()
        return self._mcp.call(commands)

    def _new_tab(self, url: str) -> str:
        output = self._command("tab new " + url)
        match = re.search(r"— tab (\d+)", output)
        if not match:
            raise RuntimeError("Browser MCP did not return a tab ID; check the browser extension connection")
        tab = match[1]
        self._tabs.add(tab)
        self._page = tab
        return tab

    def _eval(self, function: str, args: list | None = None):
        # Browser MCP 的 eval 保留反斜杠，不按 JSON 解码；命令必须占一行。
        function = " ".join(line for line in function.splitlines() if not line.lstrip().startswith("//"))
        expression = "() => ({theoResult:(" + function + ")(..." + json.dumps(args or []) + ") ?? null})"
        output = self._command("tab select " + self._page + '\neval "' + expression.replace('"', '\\"') + '"')
        marker = '{"theoResult":'
        start = output.find(marker)
        if start < 0:
            raise RuntimeError("Browser MCP returned no structured page result")
        try:
            data, _ = json.JSONDecoder().raw_decode(output[start:])
            return data["theoResult"]
        except (ValueError, KeyError) as exc:
            raise RuntimeError("Browser MCP page result was truncated or invalid") from exc

    def _wait(self, function: str, args: list | None = None, timeout: float = 30):
        deadline = time.monotonic() + timeout
        while True:
            value = self._eval(function, args)
            if value:
                return value
            if time.monotonic() >= deadline:
                raise RuntimeError("Timed out waiting for YouTube page content")
            time.sleep(0.5)

    def _check_login(self) -> None:
        value = self._wait("() => typeof window.ytcfg?.get?.('LOGGED_IN') === 'boolean' ? {loggedIn:window.ytcfg.get('LOGGED_IN')} : null")
        if not value["loggedIn"]:
            raise RuntimeError("browser profile is not logged in; run: python fetch.py --login")

    def _close_page(self) -> None:
        if self._page is not None:
            tab = self._page
            self._page = self._page_vid = self._state = None
            try:
                self._command("tab close " + tab)
                self._tabs.discard(tab)
            except Exception:
                pass  # close() 会再尝试关闭自己的标签页。

    def close(self) -> None:
        self._close_page()
        if self._mcp is not None:
            failed = []
            for tab in list(self._tabs):
                try:
                    self._command("tab close " + tab)
                    self._tabs.discard(tab)
                except Exception:
                    failed.append(tab)
            self._mcp.close()
            self._mcp = None
            if failed:
                raise RuntimeError("Browser MCP could not close task tabs: " + ", ".join(failed))

    def _open(self, video_id: str) -> dict:
        if not re.fullmatch(r"[A-Za-z0-9_-]{11}", video_id):
            raise ValueError("Invalid YouTube video ID")
        if self._page is not None and self._page_vid == video_id:
            return self._state
        self._close_page()
        try:
            self._new_tab("https://www.youtube.com/results?search_query=" + quote(video_id))
            self._check_login()
            self._wait("id => !!document.querySelector('a[href*=\"watch?v=' + id + '\"]')", [video_id])
            self._eval(_INSTALL_CAPTURE_JS, [video_id])
            selector = "a#video-title[href*='watch?v=" + video_id + "']"
            # 原生点击走 YouTube 的站内导航，保留预先安装的静音和响应捕获逻辑。
            self._command("tab select " + self._page + "\nclick " + selector)
            self._wait("() => !!window.__theoCaptions && location.pathname === '/watch'")
            self._state = self._wait(_PLAYER_STATE_JS, [video_id])
            self._page_vid = video_id
            if self._state["status"] == "LOGIN_REQUIRED":
                raise RuntimeError("LOGIN_REQUIRED: " + self._state["reason"])
            return self._state
        except Exception:
            self._close_page()
            raise

    def list_videos(self, channel_url: str, limit: int) -> list[dict]:
        self._close_page()
        try:
            self._new_tab(channel_url)
            self._check_login()
            items = self._wait(_CHANNEL_VIDEOS_JS, [limit])
            for _ in range(5):
                if len(items) >= limit:
                    break
                self._eval("() => { window.scrollTo(0,document.documentElement.scrollHeight); return true; }")
                time.sleep(1)
                items = self._eval(_CHANNEL_VIDEOS_JS, [limit])
            result = []
            for item in items:
                duration = None
                if item["duration"]:
                    duration = 0
                    for part in item["duration"].split(":"):
                        duration = duration * 60 + int(part)
                result.append({"id": item["id"], "title": item["title"], "duration": duration})
            return result
        finally:
            self._close_page()

    def details(self, video_id: str) -> dict:
        state = self._open(video_id)
        timestamp = datetime.fromisoformat(state["published"].replace("Z", "+00:00")) if state["published"] else None
        return {
            "title": state["title"], "duration": state["duration"], "description": state["description"],
            "upload_date": timestamp.strftime("%Y%m%d") if timestamp else None,
            "timestamp": int(timestamp.timestamp()) if timestamp and timestamp.tzinfo else None,
            "live_status": "is_upcoming" if state["is_upcoming"] else "is_live" if state["is_live"] else None,
        }

    def _read_capture(self, status: dict) -> dict:
        parts = []
        for offset in range(0, status["length"], CHUNK_SIZE):
            parts.append(self._eval("(start,size) => window.__theoCaptions.serialized.slice(start,start+size)", [offset, CHUNK_SIZE]))
        body = "".join(parts)
        if len(body) != status["length"]:
            raise RuntimeError("Incomplete caption transfer from Browser MCP")
        data = json.loads(body)
        events = data.get("events", [])
        if len(events) != status["eventCount"] or (events and events[-1].get("tStartMs") != status["lastStartMs"]):
            raise RuntimeError("Caption transfer integrity check failed")
        return data

    def json3(self, video_id: str) -> dict:
        try:
            state = self._open(video_id)
            if state["status"] not in (None, "OK"):
                raise RuntimeError("video not playable: " + str(state["status"]) + " " + state["reason"])
            lang = _pick_lang(state["tracks"])
            if not lang:
                raise RuntimeError("no English caption track")
            deadline = time.monotonic() + CAPTION_TIMEOUT
            ad_seconds = 0.0
            reloads = restarts = 0
            last_time = None
            last_move = last_restart = time.monotonic()
            while True:
                tick = time.monotonic()
                status = self._eval(_CAPTURE_STATUS_JS)
                if status["error"]:
                    raise RuntimeError(status["error"])
                if status["length"] is not None:
                    return self._read_capture(status)
                if tick >= deadline:
                    raise RuntimeError(f"player did not request captions in time (reloads: {reloads}, restarts: {restarts})")
                drive = self._eval(_DRIVE_PLAYER_JS, [lang])
                if drive["skip"]:
                    self._command("tab select " + self._page + "\nclick [data-theo-caption-skip='true']")
                time.sleep(1)
                elapsed = time.monotonic() - tick
                if status["ad"]:
                    extension = min(elapsed, max(0, MAX_AD_SECONDS - ad_seconds))
                    ad_seconds += elapsed
                    deadline += extension
                if status["time"] != last_time:
                    last_time, last_move = status["time"], tick
                elif tick - last_move > STALL_SECONDS and reloads < MAX_RELOADS:
                    # 重新走搜索页和站内导航，避免刷新丢失静音/捕获逻辑。
                    self._close_page()
                    self._open(video_id)
                    reloads += 1
                    last_move = last_restart = time.monotonic()
                if not status["ad"] and tick - last_restart > CAPTION_GRACE and restarts < MAX_RESTARTS:
                    self._eval("() => { document.querySelector('#movie_player')?.unloadModule?.('captions'); return true; }")
                    restarts += 1
                    last_restart = tick
        finally:
            self._close_page()


def login() -> None:
    """打开扩展连接的浏览器登录页并留给用户操作。"""
    browser = BrowserSession()
    try:
        browser._new_tab("https://accounts.google.com/ServiceLogin?service=youtube&continue=https://www.youtube.com/")
        print("请在 Browser MCP 扩展连接的浏览器中登录 YouTube；该登录标签页将保留。", flush=True)
    finally:
        # --login 明确为人工登录保留标签，退出自己的 MCP 客户端即可。
        if browser._mcp is not None:
            browser._mcp.close()

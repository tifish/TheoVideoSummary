"""抓取 Theo (t3.gg) 频道的新视频及字幕。

用法:
    python fetch.py              # 检查最新视频，下载新视频字幕
    python fetch.py --limit 30   # 扫描频道最新 30 个视频（用于补历史）
    python fetch.py --status     # 只打印待总结列表，不联网

产物:
    data/videos.json            所有视频的元数据与状态（唯一状态源）
    transcripts/<id>.txt        带 [mm:ss] 时间戳的字幕文本
    summaries/<id>.json         由 AI agent 写入的总结（见 AGENTS.md）
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DATA_FILE = ROOT / "data" / "videos.json"
TRANSCRIPT_DIR = ROOT / "transcripts"
SUMMARY_DIR = ROOT / "summaries"

CHANNEL_URL = "https://www.youtube.com/@t3dotgg/videos"
DEFAULT_LIMIT = 15          # 每次扫描的最新视频数
MIN_DURATION = 120          # 秒；更短的视为 short，跳过
PARAGRAPH_SECONDS = 30      # 字幕按约 30 秒合并为一段
MAX_TRANSCRIPT_ATTEMPTS = 5 # 字幕连续失败多少次后放弃


def log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


def load_db() -> dict:
    if DATA_FILE.exists():
        return json.loads(DATA_FILE.read_text(encoding="utf-8"))
    return {"videos": {}}


def save_db(db: dict) -> None:
    DATA_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = DATA_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(db, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(DATA_FILE)


def fmt_ts(seconds: float) -> str:
    s = int(seconds)
    h, rem = divmod(s, 3600)
    m, sec = divmod(rem, 60)
    return f"{h}:{m:02d}:{sec:02d}" if h else f"{m:02d}:{sec:02d}"


# ---------------------------------------------------------------- listing

def list_channel(limit: int) -> list[dict]:
    import yt_dlp

    opts = {"extract_flat": True, "playlistend": limit, "quiet": True, "no_warnings": True}
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(CHANNEL_URL, download=False)
    return [e for e in info.get("entries") or [] if e.get("id")]


def video_details(video_id: str) -> dict:
    import yt_dlp

    opts = {"quiet": True, "no_warnings": True, "skip_download": True}
    with yt_dlp.YoutubeDL(opts) as ydl:
        return ydl.extract_info(f"https://www.youtube.com/watch?v={video_id}", download=False)


# ---------------------------------------------------------------- transcripts

def snippets_via_api(video_id: str) -> list[tuple[float, str]]:
    from youtube_transcript_api import YouTubeTranscriptApi

    t = YouTubeTranscriptApi().fetch(video_id, languages=["en", "en-US", "en-GB"])
    return [(s.start, s.text) for s in t.snippets]


def snippets_via_ytdlp(info: dict) -> list[tuple[float, str]]:
    """备用方案：从 yt-dlp 元数据中的字幕 json3 地址下载。"""
    for key in ("subtitles", "automatic_captions"):
        tracks = (info.get(key) or {})
        for lang in ("en", "en-US", "en-orig", "en-GB"):
            for fmt in tracks.get(lang) or []:
                if fmt.get("ext") != "json3":
                    continue
                req = urllib.request.Request(fmt["url"], headers={"User-Agent": "Mozilla/5.0"})
                with urllib.request.urlopen(req, timeout=60) as r:
                    data = json.load(r)
                out = []
                for ev in data.get("events") or []:
                    text = "".join(seg.get("utf8", "") for seg in ev.get("segs") or []).strip()
                    if text:
                        out.append((ev.get("tStartMs", 0) / 1000, text))
                if out:
                    return out
    raise RuntimeError("no English subtitle track found via yt-dlp")


def to_paragraphs(snippets: list[tuple[float, str]]) -> str:
    lines, buf, start = [], [], None
    for t, text in snippets:
        text = " ".join(text.replace("\n", " ").split())
        if not text:
            continue
        if start is None:
            start = t
        buf.append(text)
        if t - start >= PARAGRAPH_SECONDS:
            lines.append(f"[{fmt_ts(start)}] {' '.join(buf)}")
            buf, start = [], None
    if buf:
        lines.append(f"[{fmt_ts(start)}] {' '.join(buf)}")
    return "\n".join(lines) + "\n"


def fetch_transcript(video_id: str, info: dict | None) -> str:
    errors = []
    try:
        return to_paragraphs(snippets_via_api(video_id))
    except Exception as e:  # noqa: BLE001
        errors.append(f"transcript-api: {type(e).__name__}: {str(e).splitlines()[0][:200]}")
    try:
        info = info or video_details(video_id)
        return to_paragraphs(snippets_via_ytdlp(info))
    except Exception as e:  # noqa: BLE001
        errors.append(f"yt-dlp: {type(e).__name__}: {str(e).splitlines()[0][:200]}")
    raise RuntimeError(" | ".join(errors))


# ---------------------------------------------------------------- main flow

def sync(limit: int) -> None:
    db = load_db()
    videos = db["videos"]
    TRANSCRIPT_DIR.mkdir(exist_ok=True)
    SUMMARY_DIR.mkdir(exist_ok=True)

    log(f"Listing latest {limit} videos from {CHANNEL_URL} ...")
    entries = list_channel(limit)
    log(f"  got {len(entries)} entries")

    for e in entries:
        vid = e["id"]
        v = videos.get(vid)
        if v is None:
            dur = e.get("duration")
            if dur is not None and dur < MIN_DURATION:
                continue
            log(f"[new] {vid} {e.get('title')}")
            try:
                info = video_details(vid)
            except Exception as ex:  # noqa: BLE001  (直播预告/会员视频等)
                log(f"  skip: cannot read details: {str(ex).splitlines()[0][:200]}")
                continue
            if info.get("live_status") in ("is_upcoming", "is_live"):
                log(f"  skip for now: live_status={info.get('live_status')}")
                continue
            if (info.get("duration") or 0) < MIN_DURATION:
                continue
            ts = info.get("timestamp")
            v = videos[vid] = {
                "id": vid,
                "title": info.get("title") or e.get("title"),
                "url": f"https://www.youtube.com/watch?v={vid}",
                "upload_date": info.get("upload_date"),  # YYYYMMDD
                "published": datetime.fromtimestamp(ts, timezone.utc).isoformat() if ts else None,
                "duration": info.get("duration"),
                "thumbnail": f"https://i.ytimg.com/vi/{vid}/mqdefault.jpg",
                "description": (info.get("description") or "")[:2000],
                "transcript": None,
                "transcript_attempts": 0,
                "added_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            }
            v["_info"] = info  # 仅本次运行使用
        else:
            info = None

        if v.get("transcript") or v.get("transcript_attempts", 0) >= MAX_TRANSCRIPT_ATTEMPTS:
            v.pop("_info", None)
            continue

        path = TRANSCRIPT_DIR / f"{vid}.txt"
        try:
            text = fetch_transcript(vid, v.pop("_info", None) or info)
            path.write_text(text, encoding="utf-8")
            v["transcript"] = path.relative_to(ROOT).as_posix()
            v.pop("transcript_error", None)
            log(f"  transcript ok: {vid} ({len(text)} chars)")
        except Exception as ex:  # noqa: BLE001
            v["transcript_attempts"] = v.get("transcript_attempts", 0) + 1
            v["transcript_error"] = str(ex)[:500]
            log(f"  transcript FAILED ({v['transcript_attempts']}/{MAX_TRANSCRIPT_ATTEMPTS}): {vid}: {ex}")
        save_db(db)
        time.sleep(1)

    for v in videos.values():
        v.pop("_info", None)
    db["last_fetch"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    save_db(db)


def pending(db: dict) -> list[dict]:
    out = [
        v for v in db["videos"].values()
        if v.get("transcript") and not (SUMMARY_DIR / f"{v['id']}.json").exists()
    ]
    return sorted(out, key=lambda v: v.get("upload_date") or "")


def print_status(db: dict) -> None:
    items = pending(db)
    failed = [v for v in db["videos"].values() if not v.get("transcript")]
    print(f"PENDING_SUMMARIES: {len(items)}")
    for v in items:
        print(f"- {v['id']} | {v.get('upload_date')} | {fmt_ts(v.get('duration') or 0)} | {v['title']}")
        print(f"  transcript: {v['transcript']}  ->  write: summaries/{v['id']}.json")
    if failed:
        print(f"NO_TRANSCRIPT_YET: {len(failed)}")
        for v in failed:
            print(f"- {v['id']} | attempts={v.get('transcript_attempts', 0)} | {v['title']}")


def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--limit", type=int, default=DEFAULT_LIMIT, help="扫描频道最新 N 个视频")
    ap.add_argument("--status", action="store_true", help="只显示待总结列表")
    args = ap.parse_args()
    if not args.status:
        sync(args.limit)
    print_status(load_db())


if __name__ == "__main__":
    main()

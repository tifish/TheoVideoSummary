"""校验 summaries/*.json 并生成 index.html（单文件，数据内嵌，可直接双击打开）。

用法:
    python build.py          # 校验 + 生成 index.html
    python build.py --check  # 只校验，不生成
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DATA_FILE = ROOT / "data" / "videos.json"
SUMMARY_DIR = ROOT / "summaries"
TEMPLATE = ROOT / "templates" / "index.template.html"
OUT = ROOT / "index.html"

TS_RE = re.compile(r"^(\d+:)?\d{1,2}:\d{2}$")


def ts_to_seconds(ts: str) -> int:
    parts = [int(p) for p in ts.split(":")]
    sec = 0
    for p in parts:
        sec = sec * 60 + p
    return sec


def validate(vid: str, s: dict) -> list[str]:
    errs = []

    def need_str(key: str, optional: bool = False):
        v = s.get(key)
        if v is None and optional:
            return
        if not isinstance(v, str) or not v.strip():
            errs.append(f"'{key}' must be a non-empty string")

    def need_ts_list(key: str, min_len: int):
        items = s.get(key)
        if not isinstance(items, list) or len(items) < min_len:
            errs.append(f"'{key}' must be a list with >= {min_len} items")
            return
        for i, it in enumerate(items):
            if not isinstance(it, dict) or not isinstance(it.get("text"), str) or not it["text"].strip():
                errs.append(f"'{key}[{i}]' must be an object with non-empty 'text'")
            elif it.get("t") is not None and not (isinstance(it["t"], str) and TS_RE.match(it["t"])):
                errs.append(f"'{key}[{i}].t' must look like 'mm:ss' or 'h:mm:ss', got {it.get('t')!r}")

    if s.get("id") != vid:
        errs.append(f"'id' must equal file name ({vid})")
    need_str("title_zh")
    need_str("tldr")
    need_ts_list("conclusions", 2)
    need_ts_list("key_points", 3)
    for key in ("takeaways", "tags"):
        v = s.get(key, [])
        if not isinstance(v, list) or not all(isinstance(x, str) and x.strip() for x in v):
            errs.append(f"'{key}' must be a list of non-empty strings")
    if not s.get("tags"):
        errs.append("'tags' must not be empty")
    need_str("sponsor", optional=True)
    return errs


def add_seconds(items: list[dict]) -> None:
    for it in items:
        if it.get("t"):
            it["s"] = ts_to_seconds(it["t"])


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true")
    args = ap.parse_args()

    db = json.loads(DATA_FILE.read_text(encoding="utf-8")) if DATA_FILE.exists() else {"videos": {}}
    videos = db["videos"]
    records, bad = [], 0

    for path in sorted(SUMMARY_DIR.glob("*.json")):
        vid = path.stem
        try:
            s = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as e:
            print(f"[INVALID] {path.name}: JSON parse error: {e}")
            bad += 1
            continue
        errs = validate(vid, s)
        if vid not in videos:
            errs.append("no matching entry in data/videos.json")
        if errs:
            bad += 1
            print(f"[INVALID] {path.name}:")
            for e in errs:
                print(f"    - {e}")
            continue
        meta = videos[vid]
        for key in ("conclusions", "key_points"):
            add_seconds(s[key])
        records.append({
            "id": vid,
            "title": meta.get("title"),
            "url": meta.get("url"),
            "date": meta.get("upload_date"),
            "duration": meta.get("duration"),
            "thumbnail": meta.get("thumbnail"),
            **{k: s.get(k) for k in ("title_zh", "tldr", "conclusions", "key_points", "takeaways", "tags", "sponsor")},
        })

    records.sort(key=lambda r: (r["date"] or "", r["id"]), reverse=True)
    pending = [v for v in videos.values() if v.get("transcript") and not (SUMMARY_DIR / f"{v['id']}.json").exists()]
    print(f"valid summaries: {len(records)}, invalid: {bad}, pending: {len(pending)}")

    if args.check:
        return 1 if bad else 0

    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "last_fetch": db.get("last_fetch"),
        "pending": len(pending),
        "videos": records,
    }
    data_js = json.dumps(payload, ensure_ascii=False).replace("</", "<\\/")
    html = TEMPLATE.read_text(encoding="utf-8").replace("/*__DATA__*/null", data_js)
    OUT.write_text(html, encoding="utf-8")
    print(f"wrote {OUT.name}")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())

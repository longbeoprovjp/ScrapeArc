"""Import from any site gallery-dl supports, using the JSON sidecar for tags.

  python -m minibooru import gallerydl URL --tag-field tags --tag-field user:source \\
      --title-field title --keyword robin --limit 20

--tag-field NAME[:CATEGORY]   metadata field holding a string or list of tags (dotted paths ok)
--title-field NAME            field used as the post title and for --keyword matching
--keyword WORD                only import files whose title/text fields match (repeatable)
Look at real metadata first:  python -m gallery_dl -j URL | head
"""
import json
import subprocess
import sys
import tempfile
from pathlib import Path

from ..core import DATA, english_tags, ingest, keyword_match
from . import register

MEDIA = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".mp4", ".webm", ".mov"}


def add_arguments(p):
    p.add_argument("url")
    p.add_argument("--tag-field", action="append", default=[])
    p.add_argument("--title-field", action="append", default=[])
    p.add_argument("--keyword", action="append", default=[])
    p.add_argument("--limit", type=int, default=20)
    p.add_argument("--sleep", type=float, default=2.0, help="seconds between requests")
    p.add_argument("--cookies", default=None, help="cookies.txt path, if the site needs one")


def dig(d, path):
    for part in path.split("."):
        d = d.get(part) if isinstance(d, dict) else None
    return d


def as_list(v):
    if v is None:
        return []
    if isinstance(v, (list, tuple, set)):
        return [str(x) for x in v]
    return [x.strip() for x in str(v).split(",") if x.strip()]


@register("gallerydl")
def run(c, args):
    stats = {"new": 0, "duplicate": 0, "filtered": 0, "error": 0}
    DATA.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as tmp:
        cmd = [sys.executable, "-m", "gallery_dl", "--dest", tmp, "--write-metadata",
               "--range", f"1-{args.limit}", "--sleep-request", str(args.sleep),
               "--download-archive", str(DATA / "gallerydl-archive.sqlite3")]
        if args.cookies:
            cmd += ["--cookies", args.cookies]
        subprocess.run(cmd + [args.url], check=False)
        for f in sorted(Path(tmp).rglob("*")):
            if f.suffix.lower() not in MEDIA:
                continue
            sidecar = f.with_name(f.name + ".json")
            meta = json.loads(sidecar.read_text()) if sidecar.exists() else {}
            texts = [str(dig(meta, k) or "") for k in args.title_field]
            if args.keyword and not keyword_match(" ".join(texts), args.keyword):
                stats["filtered"] += 1
                continue
            tags = []
            for spec in args.tag_field:
                field, _, cat = spec.partition(":")
                tags += [(t, cat or None) for t in as_list(dig(meta, field))]
            tags, _ = english_tags(tags)
            try:
                _, status = ingest(c, f, tags, title=next((t for t in texts if t), f.stem),
                                   source=str(meta.get("category", "") or args.url))
                stats[status] += 1
            except ValueError:
                stats["error"] += 1
    return stats

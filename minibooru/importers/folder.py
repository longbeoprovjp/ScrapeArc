"""Import a local folder; folder names become tags (photos/birds/robin/x.jpg -> birds, robin)."""
from pathlib import Path

from ..core import ingest
from . import register


def add_arguments(p):
    p.add_argument("path")
    p.add_argument("--tag", action="append", default=[], help="extra tag for every file")
    p.add_argument("--category", default=None, help="tag type for the folder-name tags (one from the Tags page; others become \"other\")")


@register("folder")
def run(c, args):
    root = Path(args.path).expanduser().resolve()
    stats = {"new": 0, "duplicate": 0, "error": 0}
    for f in sorted(root.rglob("*")):
        if not f.is_file():
            continue
        parts = f.relative_to(root).parts[:-1]
        tags = [(p, args.category) for p in parts] + list(args.tag)
        try:
            _, status = ingest(c, f, tags, title=f.stem, source=f"folder:{root.name}")
            stats[status] += 1
        except ValueError:
            stats["error"] += 1
    return stats

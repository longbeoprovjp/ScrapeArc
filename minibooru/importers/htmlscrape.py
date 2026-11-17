"""Scrape a listing page by URL, importing only posts whose title matches a keyword.

  python -m minibooru import htmlscrape URL --keyword robin --keyword sparrow --limit 60
Optional CSS selectors when the automatic guess is wrong: --card --title --tag --media --date
"""
from ..scraper import Options, scrape
from . import register


def add_arguments(p):
    p.add_argument("url")
    p.add_argument("--keyword", action="append", default=[], required=True)
    p.add_argument("--limit", type=int, default=20, help="number of posts to import (keeps paging until reached)")
    p.add_argument("--sleep", type=float, default=2.0)
    for s in ("card", "title", "tag", "media", "date"):
        p.add_argument(f"--{s}", default="", help=f"CSS selector for the {s}")


@register("htmlscrape")
def run(c, args):
    opt = Options(keywords=args.keyword, limit=args.limit, sleep=args.sleep,
                  card=args.card, title=args.title, tag=args.tag, media=args.media, date=args.date)
    return scrape(c, args.url, opt)

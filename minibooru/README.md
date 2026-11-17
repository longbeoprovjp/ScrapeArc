# Development notes

[← Back to the README](../README.md)

The app (package name `minibooru`) is a small tag-based media site: FastAPI + SQLite + Jinja. Read `core.py` top to
bottom to learn the data model; `scraper.py` is the site-agnostic scraper, `similar.py` the duplicate detection,
`clip.py` the bookmark saving, `web.py` the pages.

    pip install -r requirements-mini.txt
    MINIBOORU_PASSWORD=secret python -m minibooru serve --host 0.0.0.0 --port 8090   # LAN; omit password = open
    python -m minibooru import folder ~/Pictures/birds --category species
    python -m minibooru import gallerydl URL --title-field title --tag-field user:source --keyword robin --limit 20
    MINIBOORU_DATA=/path/to/store ...   # default ./data/minibooru
    pytest tests

Search: `robin -night type:video title:branch`. Aliases and implications are managed on the Tags page.

## Adding a new import source
Add `importers/mysource.py` with `add_arguments(parser)` and a `@register("mysource")` function
`run(c, args)` that calls `core.ingest(c, path, tags, title, source)`; list it in `importers/__init__.py`
and `__main__.py`. For any site gallery-dl supports, you do not need new code: use the `gallerydl`
importer and look at the fields first with `python -m gallery_dl -j URL`.

## Scraping a page by URL (web UI: /scrape)
Enter a listing-page URL and keywords; only posts whose **title** matches a keyword are imported
(title, tags and media). CLI equivalent: `python -m minibooru import htmlscrape URL --keyword robin --pages 2`.
Structure is auto-detected (`<article>` or image-links as cards; tag links inside the card; `<video>`/og:image/largest
`<img>` on the post page). If detection fails use the CSS selector fields. Obeys robots.txt, sleeps 2s between
requests, backs off on 429/5xx, refuses private/internal addresses, and remembers imported post URLs so reruns skip them.

"""Save one post from a site you are logged in to, without the server ever logging in.

A bookmarklet runs on the post page in your browser. It opens /clip (a popup on this app) and sends it the page HTML.
The popup asks the server what is on that page (parse), the bookmarklet downloads those files IN THE PAGE (so your
login cookies apply and nothing looks unusual to the site), sends them to the popup, and you confirm. Then save()
stores the post like a scraped one: same tag rules, duplicate checks and "already saved" memory.

Everything received here comes from another site's page: treat it as untrusted data (it is only parsed, never run).
"""
import re
from pathlib import Path

from bs4 import BeautifulSoup

from . import core, scraper, similar

MAX_HTML = 15 * 1024 * 1024


def parse(c, html: str, url: str) -> dict:
    """What the post page holds: title, upload date, tags (typed like the scraper does) and media URLs."""
    if not url.startswith(("http://", "https://")):
        raise ValueError("the page address must start with http:// or https://")
    soup = BeautifulSoup(html, "html.parser")
    opt = scraper.Options(keywords=[])
    scope = soup.find("article") or soup.find("main") or soup
    og = soup.find("meta", property="og:title")
    title = scraper._text(scope.find("h1") or soup.find("h1")) or (og.get("content", "") if og else "") or scraper._text(soup.title)
    links, trusted = scraper._tag_links(scope, url, url)
    if not trusted:   # unmarked links: keep URL "sections" with 2+ links (same rule as the scraper's post pages)
        counts = {}
        for _, cat in links:
            counts[cat] = counts.get(cat, 0) + 1
        links = [(l, k) for l, k in links if k and counts[k] >= 2]
    typed, _ = core.english_tags(scraper.type_by_position([l for l, _ in links], core.tag_rule(c)))
    tags, seen = [], set()
    for label, kind in typed:
        name = core.normalize_tag(label)
        if name and name not in seen:
            seen.add(name)
            tags.append({"name": name, "type": kind})
    already = c.execute("SELECT p.id FROM scraped s JOIN posts p ON p.id=s.post_id WHERE s.post_url=?", (url,)).fetchone()
    return {"title": title.strip(), "posted": scraper.find_posted(scope, soup), "tags": tags,
            "media": scraper.find_all_media(soup, url, opt, url), "already": already[0] if already else None}


def save(c, url: str, title: str, posted: str | None, tags: list[tuple[str, str]], files: list[Path],
         tmp: Path, missing: list[str] = (), log=lambda m: None) -> dict:
    """Store the post. `files` were downloaded by the browser; `missing` are URLs it could not fetch (another domain
    without CORS): the server downloads those itself, politely and only if robots.txt allows."""
    if not url.startswith(("http://", "https://")):
        raise ValueError("the page address must start with http:// or https://")
    paths = list(files)
    notes = []
    if missing:
        f = scraper.Fetcher(scraper.Options(keywords=[], sleep=1.0), log)
        try:
            for i, murl in enumerate(missing):
                d = tmp / f"srv{i}"
                d.mkdir()
                try:
                    paths.append(f.download(murl, d))
                except (scraper.ScrapeError, OSError) as e:
                    notes.append(f"could not download one file: {e}")
                except Exception as e:   # httpx errors and the like: report, keep the rest
                    notes.append(f"could not download one file: {type(e).__name__}")
        finally:
            f.client.close()
    paths = [p for p in paths if not core.is_deleted_file(c, core.file_sha(p))]
    if not paths:
        raise ValueError("no files to save (none came through, or you deleted them from this post before)")
    paths, copies = similar.drop_copies(paths)
    if copies:
        notes.append(f"{copies} copy/copies of the same picture skipped (kept the highest resolution)")
    posted = scraper.parse_date(posted) if posted else None
    pid, status = core.ingest(c, paths[0], tags, title=title.strip() or url, source=url, posted=posted)
    extra = 0
    for p in paths[1:]:
        try:
            extra += bool(core.add_media(c, pid, p))
        except ValueError as e:
            notes.append(str(e))
    c.execute("INSERT OR REPLACE INTO scraped(post_url, post_id, status) VALUES (?,?,'bookmark')", (url, pid))   # 'Bookmarked' filter
    return {"post_id": pid, "status": status, "files": 1 + extra, "notes": notes}


def clean_tags(raw, types=(core.OTHER,)) -> list[tuple[str, str]]:
    """[{"name", "type"}] from the popup -> [(name, type)], normalized; types this install doesn't have become 'other'."""
    out, seen = [], set()
    for t in raw or []:
        if not isinstance(t, dict):
            continue
        name = core.normalize_tag(str(t.get("name", "")))[:80]
        kind = t.get("type") if t.get("type") in types else core.OTHER
        if name and name not in seen and not re.search(r"\s", name):
            seen.add(name)
            out.append((name, kind))
    return out

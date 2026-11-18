"""Generic HTML scraper: give it a listing-page URL and keywords, it imports matching posts.

Flow:  listing page -> post cards (title, post link, tag links) -> keep cards whose TITLE matches
a keyword -> open each post page (media + more tags) -> download -> core.ingest.

Nothing here is site specific. It guesses the page structure; when a guess is wrong, pass CSS
selectors (card / title / tag / media) and it uses those instead.

Manners built in: obeys robots.txt, sleeps between requests, backs off on 429/5xx, gives up
after repeated errors, refuses private/loopback addresses (SSRF guard) unless allowed.
"""
import ipaddress
import json
import mimetypes
import os
import re
import socket
import tempfile
import threading
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path
from urllib.parse import urljoin, urlparse
from urllib.robotparser import RobotFileParser

import httpx
from bs4 import BeautifulSoup

from . import core, similar

UA = "Mozilla/5.0 (compatible; minibooru-scraper/1.0; personal archive)"
MEDIA_EXT = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".mp4", ".webm", ".mov"}
SKIP_IMG = re.compile(r"icon|logo|avatar|sprite|favicon|banner|placeholder|pixel", re.I)
# connected but no (complete) answer: worth a retry. Can't-connect errors (site down, offline) still fail fast.
RETRYABLE = (httpx.ReadTimeout, httpx.WriteTimeout, httpx.PoolTimeout, httpx.ReadError, httpx.RemoteProtocolError,
             ConnectionError)   # ConnectionError: the site reset or aborted the connection ("Connection reset by peer")
DOWNLOAD_TRIES = 3                # a download cut off halfway is started again this many times
ATTEMPTS = 6                # tries per request; waits 8, 16, 32, 64, 120 s in between (about 4 minutes of patience)
GIVE_UP_IN_A_ROW = 3        # requests in a row that fail every try -> the site is down, stop the run
NEXT_TEXT = {"next", "next »", "next ›", "»", "›", ">", "→", "older", "다음"}

class RunLog(list):
    """Log of one run, kept in memory. Holds only the last `cap` lines (a long run would otherwise grow without
    limit, and the progress page sends the whole log on every refresh). Also records when the run started/finished."""

    def __init__(self, cap: int = 500):
        super().__init__()
        self.cap, self.dropped = cap, 0
        self.started, self.finished = time.time(), None
        self.page = None           # listing page being read now (shown on the run page)

    def append(self, line):
        super().append(line)
        m = re.match(r"(?:Listing page|Scraping from page) (\d+)", str(line))
        if m:
            self.page = int(m.group(1))
        if len(self) > self.cap:
            n = len(self) - self.cap
            del self[:n]
            self.dropped += n

    def took(self) -> str:
        return fmt_duration((self.finished or time.time()) - self.started)


def fmt_duration(seconds) -> str:
    """45 -> '45s', 725 -> '12m 05s', 11220 -> '3h 07m', 187800 -> '2d 04h 10m'. None -> '—'."""
    if seconds is None:
        return "—"
    s = max(0, int(seconds))
    d, s = divmod(s, 86400)
    h, s = divmod(s, 3600)
    m, s = divmod(s, 60)
    if d:
        return f"{d}d {h:02d}h {m:02d}m"
    if h:
        return f"{h}h {m:02d}m"
    if m:
        return f"{m}m {s:02d}s"
    return f"{s}s"


class ScrapeError(Exception):
    pass


class Cancelled(ScrapeError):
    """The user pressed Stop."""


class TooOld(Exception):
    """A post is dated before the cutoff date."""


@dataclass
class Options:
    keywords: list[str]
    pages: int = 50000              # internal safety cap on listing pages visited (not user facing; loops are caught by `visited`)
    limit: int = 20                 # target: posts actually IMPORTED this run (scans as many as needed)
    sleep: float = 2.0
    max_mb: int = 300
    card: str = ""                  # optional CSS selectors overriding the heuristics
    title: str = ""
    tag: str = ""
    media: str = ""
    date: str = ""                  # optional CSS selector for the element holding the upload date
    stop_on_known: bool = False     # auto-rescan mode: stop the run at the first post that is already saved
    until: str = ""                 # 'YYYY-MM-DD': stop once posts are older than this (used instead of the post count)
    cancel: threading.Event | None = None   # set() to stop the run as soon as possible
    kind: str = "manual"            # 'manual' or 'auto', recorded in the run history
    single: bool = False            # the URL is ONE post (not a listing): import just that page, see scrape_single
    start_page: int = 1             # listing page to begin at (to resume a long run without re-reading the first pages)
    allow_private: bool = field(default_factory=lambda: bool(os.environ.get("MINIBOORU_ALLOW_PRIVATE")))


# ---------- polite HTTP ----------
class Fetcher:
    def __init__(self, opt: Options, log):
        self.opt, self.log = opt, log
        self.client = httpx.Client(headers={"User-Agent": UA}, follow_redirects=False, timeout=30)
        self.robots: dict[str, RobotFileParser | None] = {}
        self.last = 0.0
        self.errors = 0
        self.answered = 0            # requests the site has answered in this run (0 = never reached it: probably offline)
        self.in_post = False        # True while one post is being imported: Stop waits until it is fully saved

    def _guard(self, url: str):
        u = urlparse(url)
        if u.scheme not in ("http", "https") or not u.hostname:
            raise ScrapeError(f"only http(s) URLs are allowed: {url}")
        if self.opt.allow_private:
            return
        try:
            infos = socket.getaddrinfo(u.hostname, u.port or 80, proto=socket.IPPROTO_TCP)
        except socket.gaierror as e:
            raise ScrapeError(f"cannot resolve {u.hostname}: {e}")
        for info in infos:
            ip = ipaddress.ip_address(info[4][0])
            if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast:
                raise ScrapeError(f"refusing private/internal address for {u.hostname}")

    def _check_cancel(self):
        if not self.in_post and self.opt.cancel is not None and self.opt.cancel.is_set():
            raise Cancelled("stopped by you")

    def _sleep(self, seconds: float):
        if self.opt.cancel is not None and not self.in_post:
            self.opt.cancel.wait(seconds)
        else:
            time.sleep(seconds)
        self._check_cancel()

    def _wait(self):
        self._check_cancel()
        gap = self.opt.sleep - (time.monotonic() - self.last)
        if gap > 0:
            self._sleep(gap)
        self.last = time.monotonic()

    def _request(self, url: str, stream=False):
        """GET with manual redirects (each hop re-checked), backoff on 429/5xx and stalls.
        One request gets ATTEMPTS tries; if it still fails, that request is given up (the caller skips that post).
        Only GIVE_UP_IN_A_ROW given-up requests in a row (the site is really down) stop the whole run."""
        try:
            r = self._request_once(url, stream)
        except ScrapeError as e:
            if "giving up" not in str(e):
                raise
            self.errors += 1
            if self.errors >= GIVE_UP_IN_A_ROW:
                raise ScrapeError(f"too many failed requests in a row ({self.errors}), the site seems down: stopping this run. Last: {e}")
            raise
        self.errors = 0               # the limit is for failures in a row; one slow page in a long run is fine
        return r

    def _request_once(self, url: str, stream=False):
        net_err = None
        for attempt in range(ATTEMPTS):
            hops = 0
            cur = url
            while True:
                self._guard(cur)
                self._wait()
                req = self.client.build_request("GET", cur)
                try:
                    r = self.client.send(req, stream=stream)
                except RETRYABLE as e:        # site answered the connection but stalled or dropped it
                    r = None
                    net_err = e
                    break
                except (httpx.ConnectError, httpx.ConnectTimeout) as e:
                    if not self.answered:     # never reached the site in this run: we're offline or it's gone
                        raise
                    r = None                  # it worked until now: a blip (e.g. reset while connecting), retry
                    net_err = e
                    break
                if r.is_redirect and hops < 5:
                    cur = urljoin(cur, r.headers.get("location", ""))
                    r.close()
                    hops += 1
                    continue
                break
            last = attempt == ATTEMPTS - 1
            if r is None:                 # timeout / dropped connection: slow site or a flaky network, retry with backoff
                why = str(net_err) or type(net_err).__name__
                if last:
                    break
                wait = min(120, 2 ** (attempt + 3))
                self.log(f"  no answer from the site ({why}), waiting {wait}s and retrying")
                self._sleep(wait)
                continue
            if r.status_code in (429, 500, 502, 503, 504):
                r.close()
                why = f"server said {r.status_code}"
                if last:
                    break
                wait = min(120, 2 ** (attempt + 3))
                ra = r.headers.get("retry-after", "")
                if ra.isdigit():
                    wait = min(300, int(ra))
                self.log(f"  {why}, waiting {wait}s")
                self._sleep(wait)
                continue
            self.answered += 1
            return r
        raise ScrapeError(f"giving up on {url} after {ATTEMPTS} tries ({why})")

    def allowed(self, url: str) -> bool:
        u = urlparse(url)
        origin = f"{u.scheme}://{u.netloc}"
        if origin not in self.robots:
            rp = RobotFileParser()
            try:
                r = self._request(origin + "/robots.txt")
                if r.status_code == 200:
                    rp.parse(r.text.splitlines())
                elif r.status_code in (401, 403) or r.status_code >= 500:
                    rp.disallow_all = True
                else:
                    rp.allow_all = True
            except httpx.TransportError:
                raise  # network down: not a robots decision; let the caller retry later
            except httpx.HTTPError:
                rp.disallow_all = True
            self.robots[origin] = rp
        return self.robots[origin].can_fetch(UA, url)

    def page(self, url: str) -> BeautifulSoup:
        if not self.allowed(url):
            raise ScrapeError(f"robots.txt disallows {url}")
        r = self._request(url)
        if r.status_code != 200:
            raise ScrapeError(f"HTTP {r.status_code} for {url}")
        return BeautifulSoup(r.text, "html.parser")

    def download(self, url: str, dest_dir: Path) -> Path:
        for attempt in range(DOWNLOAD_TRIES):
            try:
                return self._download_once(url, dest_dir)
            except (httpx.TransportError, ConnectionError) as e:    # connection dropped while the file was coming in
                if attempt == DOWNLOAD_TRIES - 1:
                    raise ScrapeError(f"download kept getting cut off ({e or type(e).__name__})")
                self.log(f"  download cut off ({e or type(e).__name__}), trying again")
                self._sleep(5 * (attempt + 1))

    def _download_once(self, url: str, dest_dir: Path) -> Path:
        if not self.allowed(url):
            raise ScrapeError(f"robots.txt disallows {url}")
        r = self._request(url, stream=True)
        try:
            if r.status_code != 200:
                raise ScrapeError(f"HTTP {r.status_code} for media {url}")
            ctype = r.headers.get("content-type", "").split(";")[0].strip().lower()
            ext = Path(urlparse(url).path).suffix.lower()
            if ext not in MEDIA_EXT:
                ext = mimetypes.guess_extension(ctype) or ""
                ext = {".jpe": ".jpg"}.get(ext, ext)
            if ext not in MEDIA_EXT or not ctype.startswith(("image/", "video/", "application/octet-stream")):
                raise ScrapeError(f"not an image/video ({ctype or 'unknown type'})")
            cap = self.opt.max_mb * 1024 * 1024
            if int(r.headers.get("content-length", 0) or 0) > cap:
                raise ScrapeError(f"file larger than {self.opt.max_mb} MB")
            out = dest_dir / f"dl{ext}"
            n = 0
            with open(out, "wb") as f:
                for chunk in r.iter_bytes(1 << 16):
                    n += len(chunk)
                    if n > cap:
                        raise ScrapeError(f"file larger than {self.opt.max_mb} MB")
                    f.write(chunk)
            return out
        finally:
            r.close()


# ---------- parsing ----------
@dataclass
class Card:
    url: str
    title: str
    tags: list[tuple[str, str | None]]
    posted: str | None = None       # upload time on the source site, 'YYYY-MM-DD HH:MM:SS' UTC


DATE_FORMATS = ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d", "%Y/%m/%d", "%Y.%m.%d", "%d %B %Y", "%d %b %Y",
                "%B %d, %Y", "%b %d, %Y", "%B %d %Y", "%b %d %Y", "%m/%d/%Y", "%d/%m/%Y", "%d.%m.%Y")


def parse_date(text) -> str | None:
    """Best-effort date -> 'YYYY-MM-DD HH:MM:SS' in UTC (naive times are taken as UTC). None if unparseable."""
    t = re.sub(r"\s+", " ", str(text or "")).strip()
    t = re.sub(r"^(posted|published|uploaded|added|date)\s*(on|at)?\s*:?\s*", "", t, flags=re.I)
    if not t or len(t) > 40:
        return None
    dt = None
    if re.fullmatch(r"\d{10}(\d{3})?", t):  # unix seconds / milliseconds
        n = int(t)
        dt = datetime.fromtimestamp(n / 1000 if len(t) == 13 else n, timezone.utc)
    else:
        try:
            dt = datetime.fromisoformat(t.replace("Z", "+00:00"))
        except ValueError:
            for fmt in DATE_FORMATS:
                try:
                    dt = datetime.strptime(t, fmt)
                    break
                except ValueError:
                    continue
    if dt is None or not (1990 <= dt.year <= datetime.now().year + 1):
        return None
    if dt.tzinfo:
        dt = dt.astimezone(timezone.utc)
    return dt.strftime("%Y-%m-%d %H:%M:%S")


def parse_user_date(text) -> date | None:
    """A date typed by the user, in almost any order: 03/15/2024, 15/03/2024, 2024-03-15, Mar 15 2024, 15 March 2024, 3/15/24.
    A day above 12 settles which number is the day; when it is ambiguous (03/04/2024) it is read month first (US)."""
    t = re.sub(r"\s+", " ", str(text or "").lower().replace(",", " ")).strip()
    t = re.sub(r"(\d)(st|nd|rd|th)\b", r"\1", t)
    y = mo = d = None
    if m := re.fullmatch(r"(\d{4})[-/. ](\d{1,2})[-/. ](\d{1,2})", t) or re.fullmatch(r"(\d{4})(\d{2})(\d{2})", t):
        y, a, b = map(int, m.groups())
        mo, d = (a, b) if a <= 12 else (b, a)
    elif m := re.fullmatch(r"(\d{1,2})[-/. ](\d{1,2})[-/. ](\d{4}|\d{2})", t):
        a, b, y = map(int, m.groups())
        y = y + 2000 if y < 100 else y
        mo, d = (b, a) if a > 12 else (a, b)
    else:
        for fmt in ("%b %d %Y", "%B %d %Y", "%d %b %Y", "%d %B %Y", "%Y %b %d", "%Y %B %d"):
            try:
                dt = datetime.strptime(t.replace("sept ", "sep "), fmt)
                y, mo, d = dt.year, dt.month, dt.day
                break
            except ValueError:
                continue
    try:
        out = date(y, mo, d)
    except (TypeError, ValueError):
        return None
    return out if 1990 <= out.year <= date.today().year + 1 else None


def describe_date(d: date) -> str:
    return f"{d:%B} {d.day}, {d.year}"


def type_by_position(labels, rule=("other", "other", "other")):
    """Many sites list a post's tags in a fixed order (for example the author first). rule = the types for the first
    tag, the tags in between and the last tag (set on the Tags page). -> [(label, type)].
    Duplicates (same tag name) keep their first position; a lone tag counts as the first."""
    first, middle, last = rule
    ordered, seen = [], set()
    for label in labels:
        n = core.normalize_tag(label)
        if n and n not in seen:
            seen.add(n)
            ordered.append(label)
    if len(ordered) == 1:
        return [(ordered[0], first)]
    return [(l, first if i == 0 else last if i == len(ordered) - 1 else middle) for i, l in enumerate(ordered)]


def describe_tags(tags) -> str:
    kinds = list(dict.fromkeys(k for _, k in tags))   # in the order they appear
    parts = []
    for kind in kinds:
        names = [core.normalize_tag(l) for l, k in tags if k == kind]
        if names:
            parts.append(f"{kind}: {', '.join(names)}")
    return " · ".join(parts) or "no tags"


def _too_old(posted, until) -> bool:
    return bool(until and posted and posted[:10] < until)


DATE_ATTR = re.compile(r"date|time|publish|posted|created|uploaded", re.I)


def find_posted(scope, soup=None, selector: str = "") -> str | None:
    """Upload date of a post (or listing card): selector, <time datetime>, date-like attributes,
    meta tags, then JSON-LD. Returns 'YYYY-MM-DD HH:MM:SS' UTC or None."""
    if selector:
        for el in scope.select(selector):
            for v in [el.get("datetime"), el.get("content"), el.get_text(" ", strip=True)]:
                if v and (d := parse_date(v)):
                    return d
        return None
    for el in scope.find_all("time"):
        for v in (el.get("datetime"), el.get_text(" ", strip=True)):
            if v and (d := parse_date(v)):
                return d
    for el in scope.find_all(True):
        for k, v in el.attrs.items():
            if isinstance(v, str) and k.startswith("data-") and DATE_ATTR.search(k) \
                    and (d := parse_date(v)):
                return d
        if el.get("itemprop") and DATE_ATTR.search(str(el.get("itemprop"))):
            for v in (el.get("content"), el.get("datetime"), el.get_text(" ", strip=True)):
                if v and (d := parse_date(v)):
                    return d
    if soup is not None:
        for prop in ("article:published_time", "og:published_time", "datePublished", "article:modified_time"):
            m = soup.find("meta", attrs={"property": prop}) or soup.find("meta", attrs={"itemprop": prop}) \
                or soup.find("meta", attrs={"name": prop})
            if m and (d := parse_date(m.get("content"))):
                return d
        for js in soup.find_all("script", type="application/ld+json"):
            try:
                blob = json.loads(js.string or "")
            except ValueError:
                continue
            for item in (blob if isinstance(blob, list) else [blob]):
                if isinstance(item, dict) and (d := parse_date(item.get("datePublished") or item.get("uploadDate"))):
                    return d
    return None


def _text(el) -> str:
    return re.sub(r"\s+", " ", el.get_text(" ", strip=True)) if el else ""


def _tag_category(href: str) -> str | None:
    parts = [p for p in urlparse(href).path.split("/") if p]
    return core.normalize_tag(parts[0]) if len(parts) >= 2 else None


def _tag_links(scope, base: str, post_url: str, selector: str = ""):
    """Returns (links, trusted). trusted = the site marked them as tags (selector, rel=tag or a "tag" class)."""
    explicit = [] if selector else scope.select('a[rel~="tag"][href], a[class*="tag" i][href], [class*="tag" i] a[href]')
    trusted = bool(selector or explicit)
    links = scope.select(selector) if selector else (explicit or scope.find_all("a", href=True))
    out, seen = [], set()
    for a in links:
        href = a.get("href")
        if not href or href.startswith(("#", "javascript:", "mailto:")):
            continue
        full = urljoin(base, href)
        label = _text(a)
        if not trusted:
            if full.split("#")[0] == post_url or urlparse(full).netloc != urlparse(base).netloc:
                continue
            if not label or len(label) > 40 or a.find("img") or label.lower() in NEXT_TEXT:
                continue
            if re.fullmatch(r"\d+", label) or re.search(r"/page/\d+|[?&]page=\d+", full):
                continue
        if label and len(label) <= 80 and (label, full) not in seen:
            seen.add((label, full))
            out.append((label, _tag_category(full)))
    return out, trusted


def parse_listing(soup: BeautifulSoup, base: str, opt: Options) -> list[Card]:
    cards = []
    if opt.card:
        nodes = soup.select(opt.card)
    else:
        nodes = soup.find_all("article")
        if not nodes:  # nearest ancestor of each image-link that holds only that one image-link
            seen, nodes = set(), []
            for a in soup.find_all("a", href=True):
                if not a.find("img"):
                    continue
                node = a
                for _ in range(3):
                    parent = node.parent
                    if parent is None or len([x for x in parent.find_all("a", href=True) if x.find("img")]) > 1:
                        break
                    node = parent
                if id(node) not in seen:
                    seen.add(id(node))
                    nodes.append(node)
    for node in nodes:
        link = node if node.name == "a" and node.get("href") else None
        if link is None:
            for a in node.find_all("a", href=True):
                if a.find("img") or _text(a):
                    link = a
                    break
        if link is None:
            continue
        url = urljoin(base, link["href"]).split("#")[0]
        title = ""
        if opt.title:
            title = _text(node.select_one(opt.title))
        else:
            h = node.find(["h1", "h2", "h3", "h4", "h5"])
            img = node.find("img")
            for cand in (_text(h), link.get("title", ""), (img.get("alt", "") if img else ""), _text(link)):
                if cand.strip():
                    title = cand.strip()
                    break
        if not title:
            continue
        tags, _ = _tag_links(node, base, url, opt.tag)
        cards.append(Card(url, title, tags, find_posted(node, selector=opt.date)))
    return cards


def find_next(soup: BeautifulSoup, base: str) -> str | None:
    a = soup.find("a", rel=lambda v: v and "next" in (v if isinstance(v, list) else [v]), href=True)
    if a is None:
        for cand in soup.find_all("a", href=True):
            if _text(cand).lower() in NEXT_TEXT or "next" in " ".join(cand.get("class", [])).lower():
                a = cand
                break
    return urljoin(base, a["href"]) if a else None


def listing_page_url(url: str, n: int) -> str:
    """URL of listing page n, built the same way guess_next moves on: replace .../page/K/ or ?page=K if the URL has
    one, otherwise add /page/n/ (the common WordPress-style pattern). Page 1 is the URL itself."""
    if n <= 1:
        return url
    u = urlparse(url)
    m = re.search(r"/page/(\d+)(/?)$", u.path)
    if m:
        return u._replace(path=u.path[:m.start()] + f"/page/{n}{m.group(2)}").geturl()
    m = re.search(r"([?&]page=)(\d+)", "?" + u.query)
    if m:
        q = "?" + u.query
        return u._replace(query=(q[:m.start(2)] + str(n) + q[m.end(2):])[1:]).geturl()
    path = u.path if u.path.endswith("/") else u.path + "/"
    return u._replace(path=path + f"page/{n}/", fragment="").geturl()


def guess_next(url: str) -> str:
    """No usable 'next' link: derive the next URL from the pattern  .../page/N/  or  ?page=N  (page 1 has neither)."""
    u = urlparse(url)
    m = re.search(r"/page/(\d+)(/?)$", u.path)
    if m:
        return u._replace(path=u.path[:m.start()] + f"/page/{int(m.group(1)) + 1}{m.group(2)}").geturl()
    m = re.search(r"([?&]page=)(\d+)", u.query)
    if m:
        return u._replace(query=u.query[:m.start(2)] + str(int(m.group(2)) + 1) + u.query[m.end(2):]).geturl()
    path = u.path if u.path.endswith("/") else u.path + "/"
    return u._replace(path=path + "page/2/", query="", fragment="").geturl()


def find_media(soup: BeautifulSoup, base: str, opt: Options) -> str | None:
    def abs_(u):
        return urljoin(base, u) if u else None

    if opt.media:
        el = soup.select_one(opt.media)
        if el is not None:
            return abs_(el.get("src") or el.get("href") or el.get("data-src"))
    for sel in ("video[src]", "video source[src]"):
        el = soup.select_one(sel)
        if el:
            return abs_(el["src"])
    for prop in ("og:video:secure_url", "og:video", "og:video:url"):
        m = soup.find("meta", property=prop)
        if m and m.get("content"):
            return abs_(m["content"])
    scope = soup.find("article") or soup.find("main") or soup
    best, best_w = None, -1
    for img in scope.find_all("img"):
        src = img.get("data-src") or img.get("data-original") or img.get("src")
        if not src or src.startswith("data:") or SKIP_IMG.search(src):
            continue
        w = int(img["width"]) if str(img.get("width", "")).isdigit() else 0
        if w > best_w:
            best, best_w = src, w
    if best:
        return abs_(best)
    m = soup.find("meta", property="og:image")
    return abs_(m["content"]) if m and m.get("content") else None


NOT_CONTENT = re.compile(r"related|recommend|sidebar|comment|widget|avatar|author|profile|logo|header|footer|menu|navbar|advert|\bads?\b",
                         re.I)
MAX_PER_POST = 40


def _media_candidates(img):
    """Best URL for an <img> (lazy-load attributes first, then src/srcset)."""
    src = img.get("data-src") or img.get("data-original") or img.get("data-lazy-src") or img.get("src")
    if not src and img.get("srcset"):
        src = img["srcset"].split(",")[-1].strip().split(" ")[0]
    return src


def find_all_media(soup: BeautifulSoup, base: str, opt: Options, post_url: str = "") -> list[str]:
    """Every picture/video the post itself shows (not logos, icons, avatars or links to other posts)."""
    def abs_(u):
        return urljoin(base, u).split("#")[0] if u else None

    out: list[str] = []

    def add(u):
        u = abs_(u)
        if u and not u.startswith("data:") and u not in out:
            out.append(u)

    if opt.media:
        for el in soup.select(opt.media):
            add(el.get("src") or el.get("href") or el.get("data-src"))
        if out:
            return out[:MAX_PER_POST]
    for el in soup.select("video[src], video source[src]"):
        add(el["src"])
    for prop in ("og:video:secure_url", "og:video", "og:video:url"):
        m = soup.find("meta", property=prop)
        if m and m.get("content"):
            add(m["content"])
    scope = soup.find("article") or soup.find("main") or soup
    for img in scope.find_all("img"):
        src = _media_candidates(img)
        if not src or src.startswith("data:") or SKIP_IMG.search(src):
            continue
        # skip anything inside navigation / related-post / comment / sidebar blocks
        skip = False
        for anc in img.parents:
            if anc is scope or anc.name in (None, "[document]"):
                break
            if anc.name in ("nav", "header", "footer", "aside"):
                skip = True
                break
            if NOT_CONTENT.search(" ".join(anc.get("class", [])) + " " + (anc.get("id") or "")):
                skip = True
                break
        if skip:
            continue
        w = int(img["width"]) if str(img.get("width", "")).isdigit() else 0
        h = int(img["height"]) if str(img.get("height", "")).isdigit() else 0
        if (w and w < 120) or (h and h < 120):
            continue
        link = img.find_parent("a", href=True)
        if link:
            href = urljoin(base, link["href"]).split("#")[0]
            if Path(urlparse(href).path).suffix.lower() in MEDIA_EXT:
                src = link["href"]            # thumbnail wrapped in a link to the full-size file
            elif href not in ("", post_url, base) and urlparse(href).netloc == urlparse(base).netloc:
                continue                       # link to another page on the site = a related post
        add(src)
    if not out:
        single = find_media(soup, base, opt)
        if single:
            out.append(single)
    return out[:MAX_PER_POST]


# ---------- main entry ----------
def scrape(c, url: str, opt: Options, log=print) -> dict:
    stats = {"seen": 0, "matched": 0, "new": 0, "duplicate": 0, "already": 0, "filtered": 0, "error": 0, "stopped": 0, "cancelled": 0}
    if not [k for k in opt.keywords if k.strip()]:
        raise ScrapeError("at least one keyword is required")
    t0 = time.time()
    started = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    offline = None
    f = Fetcher(opt, log)
    page_url, visited, seen_posts = url, set(), set()
    older = 0                      # posts in a row that are dated before opt.until
    try:
        first = max(1, opt.start_page)
        if first > 1:
            page_url = listing_page_url(url, first)
            log(f"Scraping from page {first}")
        for page_no in range(first, first + opt.pages):
            if stats["new"] + stats["duplicate"] >= opt.limit:
                log(f"Reached the limit of {opt.limit} imported posts: stopping.")
                break
            if not page_url:
                log("No next page found: stopping.")
                break
            if page_url in visited:
                log(f"Next page {page_url} was already visited (the site loops back): stopping.")
                break
            visited.add(page_url)
            log(f"Listing page {page_no}: {page_url}")
            try:
                soup = f.page(page_url)
            except ScrapeError as e:
                if page_no > first and "HTTP 404" in str(e):
                    log("  no more pages")
                    break
                raise
            cards = [cd for cd in parse_listing(soup, page_url, opt) if cd.url not in seen_posts]
            log(f"  found {len(cards)} new post cards")
            if not cards:
                log("  end of listing" if page_no > first else "  none found: try the advanced selectors (card/title)"
                    + ("" if page_no > first or first == 1 else ", or check that this site has a page that far back"))
                break
            for card in cards:
                if opt.cancel is not None and opt.cancel.is_set():
                    raise Cancelled("stopped by you")
                if stats["new"] + stats["duplicate"] >= opt.limit:
                    break
                seen_posts.add(card.url)
                stats["seen"] += 1
                if _too_old(card.posted, opt.until):
                    older += 1
                    log(f"  '{card.title[:60]}' is dated {card.posted[:10]}, before the cutoff {opt.until}")
                    if older >= 2:        # two in a row, so one old pinned post at the top does not end the run
                        stats["stopped"] = 2
                        log("Reached the date limit: stopping.")
                        break
                    continue
                if opt.until and card.posted:
                    older = 0
                hits = core.keyword_match(card.title, opt.keywords)
                if not hits:
                    stats["filtered"] += 1
                    continue
                stats["matched"] += 1
                if c.execute("SELECT 1 FROM scraped s JOIN posts p ON p.id=s.post_id WHERE s.post_url=?", (card.url,)).fetchone():
                    stats["already"] += 1
                    if opt.stop_on_known:
                        stats["stopped"] = 1
                        log("  reached a post that is already saved: stopping, the rest is older")
                        break
                    continue
                log(f"+ {card.title}  [{', '.join(hits)}]" + (f"  · dated {card.posted[:10]}" if card.posted else ""))
                f.in_post = True
                try:
                    _import_post(c, f, card, opt, stats, log)
                    if opt.until:
                        older = 0
                except TooOld as e:
                    older += 1
                    log(f"  dated {str(e)[:10]}, before the cutoff {opt.until}: not imported")
                    if older >= 2:
                        stats["stopped"] = 2
                        log("Reached the date limit: stopping.")
                        break
                except ScrapeError as e:
                    if "too many" in str(e) or isinstance(e, Cancelled):
                        raise
                    stats["error"] += 1
                    log(f"  skipped: {e}")
                except (httpx.HTTPError, ValueError, ConnectionError) as e:
                    stats["error"] += 1
                    log(f"  skipped: {e or type(e).__name__}")
                finally:
                    f.in_post = False
                c.commit()
            if stats["stopped"]:
                break
            page_url = find_next(soup, page_url) or guess_next(page_url)
        else:
            log(f"Reached the safety cap of {opt.pages} listing pages: stopping. Run it again to continue further back.")
    except Cancelled:
        stats["cancelled"] = 1
        log("Stopped by you.")
    except ScrapeError as e:
        stats["error"] += 1
        log(f"Stopped: {e}")
    except httpx.TransportError as e:   # can't reach the site at all (offline?): record the run, then tell the caller
        stats["error"] += 1
        log(f"Stopped: the site can't be reached ({e or type(e).__name__})")
        offline = e
    except Exception as e:   # anything unexpected still ends the run cleanly and lands in History
        stats["error"] += 1
        log(f"Stopped by an unexpected error: {type(e).__name__}: {e}")
    finally:
        f.client.close()
    took = time.time() - t0
    c.execute("INSERT INTO scrape_runs(url, keywords, started, finished, duration_s, seen, matched, new, duplicate, already, errors, kind)"
              " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
              (url, ", ".join(opt.keywords), started, datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"), int(took),
               stats["seen"], stats["matched"], stats["new"], stats["duplicate"], stats["already"], stats["error"], opt.kind))
    c.commit()
    log("Done: " + ", ".join(f"{k}={v}" for k, v in stats.items()) + f" · took {fmt_duration(took)}")
    if offline is not None:
        raise offline
    if stats["new"] == 0 and stats["matched"] == stats["already"] and stats["matched"]:
        log("Nothing new: every matching post on the pages scanned was already imported.")
    return stats


SINGLE_MAX_TAG_LETTERS = 12      # single-post mode: longer tags are mostly not names (sentences, links, promos)
TAG_PATH = re.compile(r"/tags?/[^/]+/?$", re.I)
CATEGORY_PATH = re.compile(r"/categor(y|ies)/", re.I)     # site sections (News, Teasers...), not tags


def single_post_tags(scope, base: str, selector: str = "") -> tuple[list[str], int]:
    """Tags of one post page, strict: links the site marks rel="tag" or that point to a /tag/... page on the same
    site, never category links (rel="category" or a /category/ address); or exactly what `selector` picks. Leading '#' removed. Returns (labels, how many were too long)."""
    if selector:
        links = scope.select(selector)
    else:
        host = urlparse(base).netloc
        links = []
        for a in scope.find_all("a", href=True):
            rel = a.get("rel") or []
            full = urlparse(urljoin(base, a["href"]))
            if full.netloc != host or CATEGORY_PATH.search(full.path):
                continue
            if ("tag" in rel and "category" not in rel) or TAG_PATH.search(full.path):
                links.append(a)
    out, seen, too_long = [], set(), 0
    for a in links:
        label = re.sub(r"^#+\s*", "", _text(a)).strip()
        name = core.normalize_tag(label)
        if not name or name in seen:
            continue
        seen.add(name)
        if len(name.replace("_", "")) > SINGLE_MAX_TAG_LETTERS:
            too_long += 1
            continue
        out.append(label)
    return out, too_long


def scrape_single(c, url: str, opt: Options, log=print) -> dict:
    """Import the one post at `url` (no listing, no keyword filter). Same manners, history and duplicate rules."""
    stats = {"seen": 1, "matched": 1, "new": 0, "duplicate": 0, "already": 0, "filtered": 0, "error": 0, "stopped": 0, "cancelled": 0}
    t0 = time.time()
    started = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    f = Fetcher(opt, log)
    try:
        if c.execute("SELECT 1 FROM scraped s JOIN posts p ON p.id=s.post_id WHERE s.post_url=?", (url,)).fetchone():
            stats["already"] = 1
            log("This post is already saved: nothing to do.")
        else:
            log(f"Single post: {url}")
            soup = None if Path(urlparse(url).path).suffix.lower() in MEDIA_EXT else f.page(url)
            title = ""
            if soup is not None:
                scope = soup.find("article") or soup.find("main") or soup
                og = soup.find("meta", property="og:title")
                title = _text(scope.find("h1") or soup.find("h1")) or (og.get("content", "") if og else "") or _text(soup.title)
            f.in_post = True
            try:
                _import_post(c, f, Card(url, title.strip() or url, []), opt, stats, log, soup=soup)
            finally:
                f.in_post = False
            c.commit()
    except Cancelled:
        stats["cancelled"] = 1
        log("Stopped by you.")
    except (ScrapeError, httpx.HTTPError, ValueError, ConnectionError) as e:
        stats["error"] += 1
        log(f"Stopped: {e or type(e).__name__}")
    except Exception as e:
        stats["error"] += 1
        log(f"Stopped by an unexpected error: {type(e).__name__}: {e}")
    finally:
        f.client.close()
    took = time.time() - t0
    c.execute("INSERT INTO scrape_runs(url, keywords, started, finished, duration_s, seen, matched, new, duplicate, already, errors, kind)"
              " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
              (url, "(single post)", started, datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"), int(took),
               stats["seen"], stats["matched"], stats["new"], stats["duplicate"], stats["already"], stats["error"], opt.kind))
    c.commit()
    log("Done: " + ", ".join(f"{k}={v}" for k, v in stats.items()) + f" · took {fmt_duration(took)}")
    return stats


def _import_post(c, f: Fetcher, card: Card, opt: Options, stats: dict, log, soup=None):
    tags = list(card.tags)
    page_found = []
    posted = card.posted
    scope = None
    if Path(urlparse(card.url).path).suffix.lower() in MEDIA_EXT:
        media = [card.url]
    else:
        soup = soup if soup is not None else f.page(card.url)
        scope = soup.find("article") or soup.find("main") or soup
        media = find_all_media(soup, card.url, opt, card.url)
        posted = find_posted(soup.find("article") or soup.find("main") or soup, soup, opt.date) or posted
        if _too_old(posted, opt.until):
            raise TooOld(posted)
        page_tags, trusted = _tag_links(soup.find("article") or soup.find("main") or soup, card.url, card.url, opt.tag)
        if trusted:
            page_found = page_tags
        else:
            # unmarked links: keep ones in the same URL "section" as the card's tags, or sections with 2+ links
            known = {cat for _, cat in tags if cat}
            counts = {}
            for _, cat in page_tags:
                counts[cat] = counts.get(cat, 0) + 1
            page_found = [(l, k) for l, k in page_tags if k in known or (k and counts[k] >= 2)]
    if not media:
        raise ScrapeError("no media found on post page")
    if opt.single:
        # single-post mode: only the site's real tag links, all given the "in between" type of the rule, long ones skipped
        labels, too_long = single_post_tags(scope, card.url, opt.tag) if scope is not None else ([], 0)
        if too_long:
            log(f"  skipped {too_long} tag{'' if too_long == 1 else 's'} longer than {SINGLE_MAX_TAG_LETTERS} letters")
        tags = [(l, core.tag_rule(c)[1]) for l in labels]
    else:
        # the post page has the full tag list in the site's order; tags only on the listing card come after
        tags = type_by_position([l for l, _ in page_found] + [l for l, _ in tags], core.tag_rule(c))
    tags, non_english = core.english_tags(tags)   # after typing by position so positions stay right
    if non_english:
        log(f"  skipped {non_english} non-English tag{'' if non_english == 1 else 's'}")
    with tempfile.TemporaryDirectory() as tmp:
        path = f.download(media[0], Path(tmp))
        extras = []                       # download every file first so the database is not locked during slow network waits
        for i, murl in enumerate(media[1:], 2):
            sub_dir = Path(tmp) / str(i)
            sub_dir.mkdir()
            try:
                extras.append(f.download(murl, sub_dir))
            except ScrapeError as e:
                if "too many" in str(e):
                    raise
                log(f"  extra file {i} skipped: {e}")
            except httpx.HTTPError as e:
                log(f"  extra file {i} skipped: {e}")
        files, copies = similar.drop_copies([path] + extras)
        if copies:
            log(f"  {copies} file{'' if copies == 1 else 's'} skipped: same picture as another file of this post (kept the highest resolution)")
        kept = [p for p in files if not core.is_deleted_file(c, core.file_sha(p))]
        if len(kept) < len(files):
            log(f"  {len(files) - len(kept)} file{'' if len(files) - len(kept) == 1 else 's'} skipped: you deleted {'it' if len(files) - len(kept) == 1 else 'them'} before")
        if not kept:
            raise ScrapeError("every file of this post was deleted by you before")
        files = kept
        pid, status = core.ingest(c, files[0], tags, title=card.title, source=card.url, posted=posted)
        extra = 0
        for i, ep in enumerate(files[1:], 2):
            try:
                if core.add_media(c, pid, ep):
                    extra += 1
            except ValueError as e:
                log(f"  extra file {i} skipped: {e}")
        if len(media) > 1:
            log(f"  post has {len(media)} files, {extra} extra saved")
    stats[status] += 1
    c.execute("INSERT OR REPLACE INTO scraped(post_url, post_id, status) VALUES (?,?,'imported')", (card.url, pid))
    if not posted:
        log("  no upload date found on the site: sorting falls back to import time (try the Date selector under Advanced)")
    log(f"  {status}: post #{pid} · posted {posted or 'date unknown'} · {describe_tags(tags) if tags else 'NO TAGS (try the Tag selector under Advanced)'}")

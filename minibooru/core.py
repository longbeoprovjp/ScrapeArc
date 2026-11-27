"""Core of the mini tag site: schema, tag normalisation, ingest, search.

Concepts worth studying (same ideas Danbooru/Szurubooru use):
  * posts / tags / post_tags         many-to-many tagging
  * tag categories                   tags are grouped and coloured
  * aliases                          'jelly fish' -> jellyfish (resolved on input and search)
  * implications                     tagging 'robin' also tags 'bird'
  * sha256 de-duplication            same file twice = one post, tags merged
"""
import hashlib
import mimetypes
import os
import re
import shutil
import subprocess
import sqlite3
from contextlib import contextmanager
from pathlib import Path

from . import similar

DATA = Path(os.environ.get("MINIBOORU_DATA", "data/minibooru"))

SCHEMA = """
CREATE TABLE IF NOT EXISTS categories (name TEXT PRIMARY KEY, color TEXT NOT NULL, pos INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS tags (
  id INTEGER PRIMARY KEY, name TEXT UNIQUE NOT NULL,
  category TEXT NOT NULL DEFAULT 'other' REFERENCES categories(name));
CREATE TABLE IF NOT EXISTS aliases (alias TEXT PRIMARY KEY, tag_id INTEGER NOT NULL REFERENCES tags(id) ON DELETE CASCADE);
CREATE TABLE IF NOT EXISTS implications (
  tag_id INTEGER NOT NULL REFERENCES tags(id) ON DELETE CASCADE,
  implies_id INTEGER NOT NULL REFERENCES tags(id) ON DELETE CASCADE,
  PRIMARY KEY (tag_id, implies_id));
CREATE TABLE IF NOT EXISTS posts (
  id INTEGER PRIMARY KEY, sha256 TEXT UNIQUE NOT NULL, ext TEXT NOT NULL, mime TEXT NOT NULL,
  width INTEGER, height INTEGER, title TEXT NOT NULL DEFAULT '', source TEXT NOT NULL DEFAULT '',
  created TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE IF NOT EXISTS post_tags (
  post_id INTEGER NOT NULL REFERENCES posts(id) ON DELETE CASCADE,
  tag_id INTEGER NOT NULL REFERENCES tags(id) ON DELETE CASCADE,
  PRIMARY KEY (post_id, tag_id));
CREATE TABLE IF NOT EXISTS scraped (
  post_url TEXT PRIMARY KEY, post_id INTEGER, status TEXT NOT NULL, at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE IF NOT EXISTS scrape_runs (
  id INTEGER PRIMARY KEY, url TEXT NOT NULL, keywords TEXT NOT NULL, started TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  seen INTEGER, matched INTEGER, new INTEGER, duplicate INTEGER, already INTEGER, errors INTEGER);
CREATE TABLE IF NOT EXISTS watches (
  id INTEGER PRIMARY KEY, url TEXT NOT NULL, keywords TEXT NOT NULL, pages INTEGER NOT NULL DEFAULT 1,
  max_posts INTEGER NOT NULL DEFAULT 20, interval_min INTEGER NOT NULL DEFAULT 60, enabled INTEGER NOT NULL DEFAULT 1,
  card TEXT NOT NULL DEFAULT '', title TEXT NOT NULL DEFAULT '', tag TEXT NOT NULL DEFAULT '', media TEXT NOT NULL DEFAULT '',
  next_run REAL NOT NULL DEFAULT 0, last_run TEXT, last_result TEXT, roles TEXT NOT NULL DEFAULT '');
CREATE TABLE IF NOT EXISTS blocked_tags (name TEXT PRIMARY KEY);
CREATE TABLE IF NOT EXISTS fingerprints (sha256 TEXT PRIMARY KEY, kind TEXT NOT NULL, hashes TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS dup_flags (   -- pairs of posts that look like the same content (a < b)
  a INTEGER NOT NULL REFERENCES posts(id) ON DELETE CASCADE, b INTEGER NOT NULL REFERENCES posts(id) ON DELETE CASCADE,
  dist INTEGER, status TEXT NOT NULL DEFAULT 'open', PRIMARY KEY (a, b));
CREATE TABLE IF NOT EXISTS post_media (   -- extra files of a multi-picture post (the first file lives in posts)
  post_id INTEGER NOT NULL REFERENCES posts(id) ON DELETE CASCADE, pos INTEGER NOT NULL,
  sha256 TEXT NOT NULL, ext TEXT NOT NULL, mime TEXT NOT NULL, width INTEGER, height INTEGER,
  PRIMARY KEY (post_id, sha256));
CREATE TABLE IF NOT EXISTS deleted_media (   -- single files you removed from a post: never saved again by a scrape
  sha256 TEXT PRIMARY KEY, post_id INTEGER, at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);   -- per-install: name, ...
CREATE INDEX IF NOT EXISTS idx_post_tags_tag ON post_tags(tag_id);
CREATE INDEX IF NOT EXISTS idx_dup_flags_b ON dup_flags(b);
CREATE INDEX IF NOT EXISTS idx_scraped_post ON scraped(post_id);
"""
# Tag types are chosen per install (Tags page, or the first-run setup). "other" always exists and catches everything
# that has no type of its own. Each type has a colour and a place in the list (pos).
OTHER = "other"
OTHER_COLOR = "#9e9e9e"
TYPE_COLORS = ["#e91e63", "#ff9800", "#2196f3", "#4caf50", "#9c27b0", "#00bcd4", "#795548", "#607d8b"]
LATEST_DB_VERSION = 4
# Old databases (before tag types could be chosen) had these, plus a rule that typed scraped tags by their position
LEGACY_TYPES = [("idol", "#e91e63"), ("creator", "#ff9800"), (OTHER, OTHER_COLOR)]
_LEGACY_CREATOR = {"creator", "creators", "artist", "artists", "photographer", "photographers", "uploader", "uploaders",
                   "author", "authors"}
_LEGACY_IDOL = {"idol", "idols", "group", "groups", "person", "people", "member", "members"}


def clean_type_name(name: str) -> str:
    n = re.sub(r"\s+", "_", str(name or "").strip().lower())
    if not re.fullmatch(r"[\w-]{1,24}", n):
        raise ValueError("a tag type is 1 to 24 letters, digits, _ or -")
    return n


def tag_types(c) -> list:
    """[(name, color)] in display order; 'other' is always last."""
    rows = c.execute("SELECT name, color FROM categories ORDER BY name=?, pos, name", (OTHER,)).fetchall()
    return [(r[0], r[1]) for r in rows]


def type_names(c) -> list[str]:
    return [n for n, _ in tag_types(c)]


def tag_type(c, category) -> str:
    """A category name from a site, importer or form -> one of this install's types ('other' when it has no such type)."""
    n = re.sub(r"\s+", "_", (category or "").strip().lower())
    return n if n and c.execute("SELECT 1 FROM categories WHERE name=?", (n,)).fetchone() else OTHER


def add_type(c, name: str, color: str | None = None) -> str:
    n = clean_type_name(name)
    if c.execute("SELECT 1 FROM categories WHERE name=?", (n,)).fetchone():
        raise ValueError(f"there is already a tag type called {n}")
    if len(type_names(c)) >= 20:
        raise ValueError("at most 20 tag types")
    used = {col for _, col in tag_types(c)}
    color = color or next((col for col in TYPE_COLORS if col not in used), TYPE_COLORS[0])
    pos = c.execute("SELECT COALESCE(MAX(pos), 0) + 1 FROM categories WHERE name<>?", (OTHER,)).fetchone()[0]
    c.execute("INSERT INTO categories(name, color, pos) VALUES (?, ?, ?)", (n, _color(color), pos))
    return n


def _color(color: str) -> str:
    if not re.fullmatch(r"#[0-9a-fA-F]{6}", color or ""):
        raise ValueError("a colour looks like #e91e63")
    return color.lower()


def edit_type(c, name: str, new_name: str | None = None, color: str | None = None) -> str:
    """Rename and/or recolour a type (its tags and the scraping rule follow the new name)."""
    old = tag_type(c, name)
    if old != name:
        raise ValueError("no such tag type")
    if color:
        c.execute("UPDATE categories SET color=? WHERE name=?", (_color(color), old))
    if new_name is None or clean_type_name(new_name) == old:
        return old
    if old == OTHER:
        raise ValueError("'other' can't be renamed")
    new = clean_type_name(new_name)
    if c.execute("SELECT 1 FROM categories WHERE name=?", (new,)).fetchone():
        raise ValueError(f"there is already a tag type called {new}")
    rule = tag_rule(c)
    c.execute("INSERT INTO categories(name, color, pos) SELECT ?, color, pos FROM categories WHERE name=?", (new, old))
    c.execute("UPDATE tags SET category=? WHERE category=?", (new, old))
    c.execute("DELETE FROM categories WHERE name=?", (old,))
    set_tag_rule(c, *[new if k == old else k for k in rule])
    return new


def delete_type(c, name: str) -> int:
    """Remove a type; its tags become 'other'. Returns how many tags moved."""
    if name == OTHER:
        raise ValueError("'other' can't be deleted")
    if not c.execute("SELECT 1 FROM categories WHERE name=?", (name,)).fetchone():
        raise ValueError("no such tag type")
    moved = c.execute("UPDATE tags SET category=? WHERE category=?", (OTHER, name)).rowcount
    c.execute("DELETE FROM categories WHERE name=?", (name,))
    set_tag_rule(c, *[OTHER if k == name else k for k in tag_rule(c)])
    return moved


def set_tag_types(c, names) -> None:
    """Make sure these types exist (used by the first-run setup); existing ones are kept."""
    have = set(type_names(c))
    for n in names:
        if clean_type_name(n) not in have:
            add_type(c, n)


# Scraped tags can be typed by their position in the post's tag list (many sites list them in a fixed order,
# e.g. author first). The rule names the type for the first tag, the tags in between, and the last tag.
def tag_rule(c) -> tuple[str, str, str]:
    raw = (get_setting(c, "tag_rule") or "").split(",")
    names = set(type_names(c))
    return tuple(k if k in names else OTHER for k in (raw + [OTHER] * 3)[:3])


def set_tag_rule(c, first: str, middle: str, last: str) -> None:
    names = set(type_names(c))
    set_setting(c, "tag_rule", ",".join(k if k in names else OTHER for k in (first, middle, last)))


def _legacy_type(category) -> str:
    c = (category or "").strip().lower()
    return "creator" if c in _LEGACY_CREATOR else "idol" if c in _LEGACY_IDOL else OTHER


def _migrate_tag_types(c):
    """Old databases only: fold every old category into idol / creator / other and drop the unused ones."""
    version = c.execute("PRAGMA user_version").fetchone()[0]
    if version >= 2:
        return
    names = [n for n, _ in LEGACY_TYPES]
    c.executemany("INSERT OR IGNORE INTO categories(name, color) VALUES (?, ?)", LEGACY_TYPES)
    c.executemany("UPDATE categories SET color=? WHERE name=?", [(col, n) for n, col in LEGACY_TYPES])
    for (cat,) in c.execute("SELECT DISTINCT category FROM tags").fetchall():
        if cat not in names:
            c.execute("UPDATE tags SET category=? WHERE category=?", (_legacy_type(cat), cat))
    c.execute(f"DELETE FROM categories WHERE name NOT IN ({','.join('?' * len(names))})", names)
    c.execute("PRAGMA user_version = 2")


def _migrate_type_order(c):
    """Databases from before types could be chosen keep their idol / creator / other types and the position rule
    they always used (first tag = creator, tags in between = idol, last = other)."""
    if c.execute("PRAGMA user_version").fetchone()[0] >= 4:
        return
    names = set(type_names(c))
    for i, n in enumerate(("idol", "creator")):
        c.execute("UPDATE categories SET pos=? WHERE name=?", (i + 1, n))
    if {"idol", "creator"} <= names and get_setting(c, "tag_rule") is None:
        set_setting(c, "tag_rule", "creator,idol,other")
    c.execute("PRAGMA user_version = 4")


def _migrate_upload_tag(c):
    """Once per database: posts uploaded through the web page before the 'upload' tag existed get that tag."""
    if c.execute("PRAGMA user_version").fetchone()[0] >= 3:
        return
    ids = [r[0] for r in c.execute("SELECT id FROM posts WHERE source='upload'")]
    for pid in ids:
        add_tags(c, pid, [UPLOAD_TAG])
    c.execute("PRAGMA user_version = 3")


DATA_MARKER = ".scrapearc-data"   # says "this folder is the real archive" (it's missing when the encrypted store is locked)


def _has_posts(db: Path) -> bool:
    """Read-only look: does this database hold at least one post? (Never creates the file.)"""
    if not db.is_file():
        return False
    try:
        c = sqlite3.connect(db.as_uri() + "?mode=ro", uri=True, timeout=5)
        try:
            return c.execute("SELECT 1 FROM posts LIMIT 1").fetchone() is not None
        finally:
            c.close()
    except sqlite3.Error:
        return False


def data_ready(data=None) -> bool:
    """False when MINIBOORU_DATA_REQUIRED is set and the data folder isn't the real archive (e.g. the server rebooted and
    the encrypted store isn't unlocked yet, so the folder is missing or empty). Then nothing may be written there.
    A folder that already holds posts gets the marker the first time; without the setting, a new install gets it too."""
    base = Path(data) if data else DATA
    if (base / DATA_MARKER).is_file():
        return True
    if not _has_posts(base / "db.sqlite") and os.environ.get("MINIBOORU_DATA_REQUIRED"):
        return False
    base.mkdir(parents=True, exist_ok=True)
    (base / DATA_MARKER).write_text("This folder is the archive's data. The app refuses to start where this file is missing "
                                    "(when MINIBOORU_DATA_REQUIRED is set). Don't delete it.\n")
    return True


@contextmanager
def connect(data=None):
    base = Path(data) if data else DATA
    base.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(base / "db.sqlite", timeout=30)   # wait for another writer instead of failing
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA foreign_keys = ON")
    fresh = c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='posts'").fetchone() is None
    c.executescript(SCHEMA)
    have = {t: {r[1] for r in c.execute(f"PRAGMA table_info({t})")} for t in ("watches", "posts", "scrape_runs", "categories")}
    for table, col, ddl in (("watches", "roles", "TEXT NOT NULL DEFAULT ''"),     # migrate older databases
                            ("watches", "date", "TEXT NOT NULL DEFAULT ''"),
                            ("watches", "until", "TEXT NOT NULL DEFAULT ''"),     # stop at this date (YYYY-MM-DD), '' = use post count
                            ("scrape_runs", "kind", "TEXT NOT NULL DEFAULT 'manual'"),
                            ("posts", "posted", "TEXT"),                           # upload time on the source site (UTC)
                            ("posts", "favorite", "INTEGER NOT NULL DEFAULT 0"),
                            ("categories", "pos", "INTEGER NOT NULL DEFAULT 0")):        # order of tag types
        if col not in have[table]:
            c.execute(f"ALTER TABLE {table} ADD COLUMN {col} {ddl}")
    if "finished" not in have["scrape_runs"]:
        # runs used to be saved only at the end, so their 'started' value is really the finish time; start unknown
        c.execute("ALTER TABLE scrape_runs ADD COLUMN finished TEXT")
        c.execute("ALTER TABLE scrape_runs ADD COLUMN duration_s INTEGER")   # NULL = not recorded (older runs)
        c.execute("UPDATE scrape_runs SET finished=started")
    # list order (upload date): made after the columns above exist; an existing index costs nothing here
    c.execute("CREATE INDEX IF NOT EXISTS idx_posts_when ON posts(COALESCE(posted, created), id)")
    if fresh:   # a new install starts with only 'other'; its owner picks the rest
        c.execute("INSERT OR IGNORE INTO categories(name, color) VALUES (?, ?)", (OTHER, OTHER_COLOR))
        c.execute(f"PRAGMA user_version = {LATEST_DB_VERSION}")
        # colours chosen in Customize are kept per browser under this id, so a new install never inherits another's
        c.execute("INSERT OR IGNORE INTO settings(key, value) VALUES ('install_id', ?)", (os.urandom(4).hex(),))
    elif not c.execute("SELECT 1 FROM categories WHERE name=?", (OTHER,)).fetchone():   # only write when missing
        c.execute("INSERT OR IGNORE INTO categories(name, color) VALUES (?, ?)", (OTHER, OTHER_COLOR))
    _migrate_tag_types(c)
    _migrate_upload_tag(c)
    _migrate_type_order(c)
    try:
        yield c
        c.commit()
    finally:
        c.close()


# ---------- tag names ----------
def normalize_tag(name: str) -> str:
    """lowercase, whitespace -> '_', strip characters with special meaning in search."""
    name = re.sub(r"[\s]+", "_", name.strip().lower())
    name = re.sub(r"[,:\\/*?\"<>|]+", "_", name)
    return re.sub(r"_+", "_", name).strip("_-")


def is_english_tag(name: str) -> bool:
    """True when every letter in the tag is plain ASCII (Korean, Japanese, Cyrillic, accented... are rejected)."""
    return bool(name.strip()) and all(ch.isascii() or not ch.isalpha() for ch in name)


def english_tags(tags):
    """Keep only English tags from an iterable of 'name' or ('name', 'category'). Returns (kept, dropped_count)."""
    kept, dropped = [], 0
    for t in tags:
        if is_english_tag(t if isinstance(t, str) else t[0]):
            kept.append(t)
        else:
            dropped += 1
    return kept, dropped


def lookup_tag(c, name: str):
    """tag id for a name or alias, or None. Never creates anything."""
    n = normalize_tag(name)
    row = c.execute("SELECT tag_id FROM aliases WHERE alias=?", (n,)).fetchone()
    if row:
        return row[0]
    row = c.execute("SELECT id FROM tags WHERE name=?", (n,)).fetchone()
    return row[0] if row else None


def get_or_create_tag(c, name: str, category: str | None = None) -> int | None:
    n = normalize_tag(name)
    if not n:
        return None
    tid = lookup_tag(c, n)
    kind = tag_type(c, category)
    if tid is None:
        cur = c.execute("INSERT INTO tags(name, category) VALUES (?, ?)", (n, kind))
        return cur.lastrowid
    if kind != OTHER:  # an 'other' tag gets a type when a post shows it as one; a typed tag is never downgraded
        c.execute("UPDATE tags SET category=? WHERE id=? AND category='other'", (kind, tid))
    return tid


def expand_implications(c, tag_ids):
    seen, stack = set(), list(tag_ids)
    while stack:
        t = stack.pop()
        if t in seen:
            continue
        seen.add(t)
        stack += [r[0] for r in c.execute("SELECT implies_id FROM implications WHERE tag_id=?", (t,))]
    return seen


def add_tags(c, post_id: int, tags) -> None:
    """tags: iterable of 'name' or ('name', 'category')."""
    ids = set()
    blocked = {r[0] for r in c.execute("SELECT name FROM blocked_tags")}
    for t in tags:
        name, cat = (t, None) if isinstance(t, str) else t
        if normalize_tag(name) in blocked:
            continue
        tid = get_or_create_tag(c, name, cat)
        if tid:
            ids.add(tid)
    for tid in expand_implications(c, ids):
        c.execute("INSERT OR IGNORE INTO post_tags VALUES (?, ?)", (post_id, tid))


def set_tags(c, post_id: int, names) -> None:
    c.execute("DELETE FROM post_tags WHERE post_id=?", (post_id,))
    add_tags(c, post_id, names)


def bulk_tags(c, post_ids, add=(), remove=()) -> tuple[int, int]:
    """Add and/or remove tags on many posts at once ("Select posts" -> Edit tags). Removing goes first, so a tag in both
    lists ends up on the posts. Removing only takes off the tag named (or the tag an alias points to), not what it implies.
    Returns (posts that exist, tag links taken off)."""
    ids = [r[0] for r in c.execute(f"SELECT id FROM posts WHERE id IN ({','.join('?' * len(post_ids))})", list(post_ids))] \
        if post_ids else []
    gone = 0
    drop = {t for t in (lookup_tag(c, n) for n in remove) if t is not None}
    for pid in ids:
        for tid in drop:
            gone += c.execute("DELETE FROM post_tags WHERE post_id=? AND tag_id=?", (pid, tid)).rowcount
        if add:
            add_tags(c, pid, add)
    return len(ids), gone


def add_implication(c, tag: str, implies: str) -> None:
    a, b = get_or_create_tag(c, tag), get_or_create_tag(c, implies)
    if a is None or b is None or a == b:
        raise ValueError("invalid implication")
    if a in expand_implications(c, [b]):
        raise ValueError("implication would create a loop")
    c.execute("INSERT OR IGNORE INTO implications VALUES (?, ?)", (a, b))
    # apply retroactively
    for (pid,) in c.execute("SELECT post_id FROM post_tags WHERE tag_id=?", (a,)).fetchall():
        c.execute("INSERT OR IGNORE INTO post_tags VALUES (?, ?)", (pid, b))


def add_alias(c, alias: str, target: str) -> None:
    al, tid = normalize_tag(alias), get_or_create_tag(c, target)
    if not al or tid is None:
        raise ValueError("invalid alias")
    if c.execute("SELECT 1 FROM tags WHERE name=?", (al,)).fetchone():
        raise ValueError(f"'{al}' is already a real tag; merge it first")
    c.execute("INSERT OR REPLACE INTO aliases VALUES (?, ?)", (al, tid))


def delete_tag(c, name: str, block: bool = False) -> bool:
    """Delete a tag everywhere (posts keep existing, just lose it). block=True also stops future imports re-adding it."""
    tid = lookup_tag(c, name)
    n = normalize_tag(name)
    if tid is not None:
        row = c.execute("SELECT name FROM tags WHERE id=?", (tid,)).fetchone()
        n = row[0]
        c.execute("DELETE FROM tags WHERE id=?", (tid,))   # cascades to post_tags, aliases, implications
    if block and n:
        c.execute("INSERT OR IGNORE INTO blocked_tags VALUES (?)", (n,))
    return tid is not None


def post_tags(c, post_id: int):
    return c.execute("""SELECT t.name, t.category, cat.color FROM post_tags pt
        JOIN tags t ON t.id=pt.tag_id JOIN categories cat ON cat.name=t.category
        WHERE pt.post_id=? ORDER BY t.category, t.name""", (post_id,)).fetchall()


def tags_for_posts(c, post_ids) -> dict:
    """{post_id: [(name, color), ...]} for many posts in one query (for grids)."""
    ids = list(post_ids)
    out = {i: [] for i in ids}
    if ids:
        q = ",".join("?" * len(ids))
        for r in c.execute(f"""SELECT pt.post_id, t.name, cat.color FROM post_tags pt
            JOIN tags t ON t.id=pt.tag_id JOIN categories cat ON cat.name=t.category
            WHERE pt.post_id IN ({q}) ORDER BY t.category, t.name""", ids):
            out[r[0]].append((r[1], r[2]))
    return out


# ---------- ingest ----------
def video_thumb(src: Path, out: Path):
    """Best effort: grab a frame with ffmpeg and read the size with ffprobe. Returns (w, h) or (None, None)."""
    if not shutil.which("ffmpeg"):
        return None, None
    out.parent.mkdir(exist_ok=True)
    try:
        for ts in ("1", "0"):  # short clips have no frame at 1s
            subprocess.run(["ffmpeg", "-y", "-v", "error", "-ss", ts, "-i", str(src), "-frames:v", "1",
                            "-vf", "scale=320:320:force_original_aspect_ratio=decrease", str(out)],
                           check=False, timeout=60, stdin=subprocess.DEVNULL)
            if out.exists() and out.stat().st_size:
                break
        probe = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
                                "stream=width,height", "-of", "csv=p=0", str(src)],
                               capture_output=True, text=True, timeout=30).stdout.strip().split(",")
        return int(probe[0]), int(probe[1])
    except (subprocess.SubprocessError, ValueError, IndexError):
        return None, None



def _store(path: Path, base: Path):
    """Copy a media file into the store and make its thumbnail. Returns (sha, ext, mime, width, height, existed)."""
    mime = mimetypes.guess_type(path.name)[0] or ""
    if not mime.startswith(("image/", "video/")) or mime == "image/svg+xml":   # SVG can carry script
        raise ValueError(f"unsupported file type: {path.name}")
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    sha = h.hexdigest()
    ext = path.suffix.lower().lstrip(".")
    dest = base / "files" / sha[:2] / f"{sha}.{ext}"
    if dest.exists():
        return sha, ext, mime, None, None, True
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(path, dest)
    width = height = None
    if mime.startswith("image/"):
        try:
            from PIL import Image
            with Image.open(dest) as im:
                width, height = im.size
                th = im.convert("RGB")
                th.thumbnail((320, 320))
                (base / "thumbs").mkdir(exist_ok=True)
                th.save(base / "thumbs" / f"{sha}.jpg", quality=82)
        except Exception:
            dest.unlink(missing_ok=True)
            raise ValueError(f"unreadable image: {path.name}")
    else:
        width, height = video_thumb(dest, base / "thumbs" / f"{sha}.jpg")
    return sha, ext, mime, width, height, False


def ingest(c, path, tags=(), title="", source="", data=None, posted=None):
    """Store a media file. Returns (post_id, 'new' | 'duplicate'). Duplicates get tags merged.
    posted: 'YYYY-MM-DD HH:MM:SS' UTC, when the post was uploaded on the original site (optional)."""
    path = Path(path)
    base = Path(data) if data else DATA
    sha, ext, mime, width, height, _ = _store(path, base)
    row = c.execute("SELECT id FROM posts WHERE sha256=? UNION ALL SELECT post_id FROM post_media WHERE sha256=? LIMIT 1",
                    (sha, sha)).fetchone()   # also inside multi-file posts (e.g. merged ones)
    if row:
        if c.execute("SELECT source FROM posts WHERE id=?", (row[0],)).fetchone()[0] != "upload":
            tags = [t for t in tags if (t if isinstance(t, str) else t[0]) != UPLOAD_TAG]   # a scraped post stays unmarked
        add_tags(c, row[0], tags)
        if posted:
            c.execute("UPDATE posts SET posted=? WHERE id=? AND posted IS NULL", (posted, row[0]))
        return row[0], "duplicate"
    cur = c.execute("INSERT INTO posts(sha256, ext, mime, width, height, title, source, posted) VALUES (?,?,?,?,?,?,?,?)",
                    (sha, ext, mime, width, height, title, source, posted))
    add_tags(c, cur.lastrowid, tags)
    similar.check_file(c, cur.lastrowid, base, sha, ext, mime)
    return cur.lastrowid, "new"


def add_media(c, post_id: int, path, data=None) -> bool:
    """Attach one more picture/video to an existing post. False if the post already has that exact file."""
    path = Path(path)
    base = Path(data) if data else DATA
    sha, ext, mime, width, height, existed = _store(path, base)
    if c.execute("SELECT 1 FROM posts WHERE id=? AND sha256=?", (post_id, sha)).fetchone() or \
       c.execute("SELECT 1 FROM post_media WHERE post_id=? AND sha256=?", (post_id, sha)).fetchone():
        return False
    if existed:   # file already stored for another post: reuse its size info
        r = c.execute("SELECT width, height FROM posts WHERE sha256=? UNION ALL SELECT width, height FROM post_media WHERE sha256=?",
                      (sha, sha)).fetchone()
        width, height = (r[0], r[1]) if r else (None, None)
    pos = c.execute("SELECT COALESCE(MAX(pos), 0) + 1 FROM post_media WHERE post_id=?", (post_id,)).fetchone()[0]
    c.execute("INSERT INTO post_media VALUES (?,?,?,?,?,?,?)", (post_id, pos, sha, ext, mime, width, height))
    similar.check_file(c, post_id, base, sha, ext, mime)
    return True


def post_files(c, pid: int):
    """Every file of a post, first one first: [{sha, ext, mime, w, h, url, thumb}]."""
    p = c.execute("SELECT sha256, ext, mime, width, height FROM posts WHERE id=?", (pid,)).fetchone()
    if not p:
        return []
    rows = [tuple(p)] + [tuple(r) for r in c.execute(
        "SELECT sha256, ext, mime, width, height FROM post_media WHERE post_id=? ORDER BY pos", (pid,))]
    return [{"sha": s, "ext": e, "mime": m, "w": w, "h": h, "is_image": m.startswith("image/"),
             "url": f"/files/{s[:2]}/{s}.{e}", "thumb": f"/thumbs/{s}.jpg"} for s, e, m, w, h in rows]


def delete_post(c, pid: int, data=None) -> bool:
    """Remove a post, its files and its scrape record (so it can be scraped again). Tags are kept."""
    p = c.execute("SELECT sha256, ext FROM posts WHERE id=?", (pid,)).fetchone()
    if not p:
        return False
    files = [(p[0], p[1])] + [tuple(r) for r in c.execute("SELECT sha256, ext FROM post_media WHERE post_id=?", (pid,))]
    c.execute("DELETE FROM posts WHERE id=?", (pid,))          # cascades to post_media
    c.execute("DELETE FROM scraped WHERE post_id=?", (pid,))
    base = Path(data) if data else DATA
    for sha, ext in files:
        _remove_file_if_unused(c, base, sha, ext)
    return True


def merge_posts(c, ids) -> int | None:
    """Merge posts into the first one (in the order given): it gets every file of the others (after its own, in order,
    each file once), all their tags, and the favorite star if any had it; their scrape records point at it, so a
    rescan doesn't save them again. The other posts are removed; no file is deleted. Returns the merged post's id."""
    ids = list(dict.fromkeys(int(i) for i in ids))
    ids = [i for i in ids if c.execute("SELECT 1 FROM posts WHERE id=?", (i,)).fetchone()]
    if len(ids) < 2:
        return ids[0] if ids else None
    target, others = ids[0], ids[1:]
    have = {f["sha"] for f in post_files(c, target)}
    pos = c.execute("SELECT COALESCE(MAX(pos), 0) FROM post_media WHERE post_id=?", (target,)).fetchone()[0]
    for pid in others:
        for f in post_files(c, pid):
            if f["sha"] not in have:
                pos += 1
                have.add(f["sha"])
                c.execute("INSERT INTO post_media VALUES (?,?,?,?,?,?,?)",
                          (target, pos, f["sha"], f["ext"], f["mime"], f["w"], f["h"]))
    q = ",".join("?" * len(others))
    c.execute(f"INSERT OR IGNORE INTO post_tags SELECT ?, tag_id FROM post_tags WHERE post_id IN ({q}) AND tag_id IS NOT ?",
              (target, *others, lookup_tag(c, UPLOAD_TAG)))   # 'upload' stays only if the merged post is an upload itself
    c.execute(f"UPDATE posts SET favorite=MAX(favorite, (SELECT MAX(favorite) FROM posts WHERE id IN ({q}))) WHERE id=?",
              (*others, target))
    c.execute(f"UPDATE scraped SET post_id=? WHERE post_id IN ({q})", (target, *others))
    c.execute(f"UPDATE deleted_media SET post_id=? WHERE post_id IN ({q})", (target, *others))
    c.execute(f"DELETE FROM posts WHERE id IN ({q})", others)   # cascades: their post_media, post_tags, dup_flags
    return target


def resolve_duplicate(c, keep: int, drop: int, data=None) -> bool:
    """`drop` is a copy of `keep`. `keep` gets drop's tags (but not the upload tag: that one only stays on a post that was
    uploaded itself), its favorite star and its scrape records, and drop's file is remembered as removed, so a rescan
    doesn't save the copy again. Then drop is deleted. Returns False if either post is gone."""
    if keep == drop or c.execute("SELECT COUNT(*) FROM posts WHERE id IN (?,?)", (keep, drop)).fetchone()[0] != 2:
        return False
    c.execute("INSERT OR IGNORE INTO post_tags SELECT ?, tag_id FROM post_tags WHERE post_id=? AND tag_id IS NOT ?",
              (keep, drop, lookup_tag(c, UPLOAD_TAG)))
    c.execute("UPDATE posts SET favorite=MAX(favorite, (SELECT favorite FROM posts WHERE id=?)) WHERE id=?", (drop, keep))
    c.execute("UPDATE scraped SET post_id=? WHERE post_id=?", (keep, drop))
    c.execute("UPDATE deleted_media SET post_id=? WHERE post_id=?", (keep, drop))
    keep_shas = {f["sha"] for f in post_files(c, keep)}
    for f in post_files(c, drop):
        if f["sha"] not in keep_shas:
            c.execute("INSERT OR REPLACE INTO deleted_media(sha256, post_id) VALUES (?, ?)", (f["sha"], keep))
    delete_post(c, drop, data)
    return True


SURE_MAX_DIST = 2   # of 64 fingerprint bits; IMG_MAX_DIST (5) flags a pair, this close counts as certain


def sure_duplicates(c, data=None) -> list[tuple[int, int]]:
    """Open look-alike pairs that are certainly the same picture: two single-file picture posts, fingerprints at most
    SURE_MAX_DIST bits apart and the same shape. Returns (keep, drop) pairs: keep the bigger picture, then the bigger
    file, then the older post. Videos, multi-file posts and less certain pairs are left for review by hand."""
    base = Path(data) if data else DATA
    rows = c.execute("""SELECT f.a, f.b FROM dup_flags f JOIN posts pa ON pa.id=f.a JOIN posts pb ON pb.id=f.b
        WHERE f.status='open' AND f.dist <= ? AND pa.mime LIKE 'image/%' AND pb.mime LIKE 'image/%'
          AND pa.width > 0 AND pa.height > 0 AND pb.width > 0 AND pb.height > 0
          AND ABS(1.0 * pa.width / pa.height - 1.0 * pb.width / pb.height) < 0.02
          AND NOT EXISTS (SELECT 1 FROM post_media m WHERE m.post_id IN (f.a, f.b))
        ORDER BY f.dist, f.a, f.b""", (SURE_MAX_DIST,)).fetchall()

    def rank(pid):
        p = c.execute("SELECT sha256, ext, width, height FROM posts WHERE id=?", (pid,)).fetchone()
        f = base / "files" / p[0][:2] / f"{p[0]}.{p[1]}"
        return (p[2] * p[3], f.stat().st_size if f.exists() else 0, -pid)

    out, gone = [], set()
    for a, b in rows:
        if a in gone or b in gone:   # chains (a~b, b~c): each post is removed at most once
            continue
        keep, drop = (a, b) if rank(a) >= rank(b) else (b, a)
        out.append((keep, drop))
        gone.add(drop)
    return out


def _remove_file_if_unused(c, base: Path, sha: str, ext: str) -> bool:
    if c.execute("SELECT 1 FROM posts WHERE sha256=? UNION SELECT 1 FROM post_media WHERE sha256=?", (sha, sha)).fetchone():
        return False   # another post still uses this file
    (base / "files" / sha[:2] / f"{sha}.{ext}").unlink(missing_ok=True)
    (base / "thumbs" / f"{sha}.jpg").unlink(missing_ok=True)
    c.execute("DELETE FROM fingerprints WHERE sha256=?", (sha,))
    return True


def collapse_copies(c, pid: int, data=None, apply: bool = True) -> int:
    """For a post saved before copies were caught at import: keep only the highest-resolution copy of each picture
    (see similar.drop_copies). The post's first file becomes that best copy when needed. Returns files removed
    (or that would be removed, with apply=False)."""
    base = Path(data) if data else DATA
    files = post_files(c, pid)
    if len(files) < 2:
        return 0
    paths = [base / "files" / f["sha"][:2] / f"{f['sha']}.{f['ext']}" for f in files]
    present = [p for p in paths if p.exists()]
    if len(present) != len(paths):
        return 0       # a file is missing on disk: leave this post alone
    kept, dropped = similar.drop_copies(paths)
    if not dropped or not apply:
        return dropped
    by_path = dict(zip(paths, files))
    keep = [by_path[p] for p in kept]
    first = keep[0]
    if first["sha"] != files[0]["sha"]:
        other = c.execute("SELECT id FROM posts WHERE sha256=? AND id!=?", (first["sha"], pid)).fetchone()
        if other:
            return 0   # that file is another post's main file (posts.sha256 is unique): leave this post alone
        c.execute("UPDATE posts SET sha256=?, ext=?, mime=?, width=?, height=? WHERE id=?",
                  (first["sha"], first["ext"], first["mime"], first["w"], first["h"], pid))
    c.execute("DELETE FROM post_media WHERE post_id=?", (pid,))
    c.executemany("INSERT INTO post_media VALUES (?,?,?,?,?,?,?)",
                  [(pid, i, f["sha"], f["ext"], f["mime"], f["w"], f["h"]) for i, f in enumerate(keep[1:], 1)])
    kept_shas = {f["sha"] for f in keep}
    for f in files:
        if f["sha"] not in kept_shas:
            _remove_file_if_unused(c, base, f["sha"], f["ext"])
    return dropped


def file_sha(path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def is_deleted_file(c, sha: str) -> bool:
    return c.execute("SELECT 1 FROM deleted_media WHERE sha256=?", (sha,)).fetchone() is not None


def remove_file(c, pid: int, sha: str, data=None) -> None:
    """Delete one picture/video from a post that has several. It is remembered (deleted_media), so a later scrape
    never saves that file again. If it was the post's first file, the next one takes its place."""
    files = post_files(c, pid)
    if not any(f["sha"] == sha for f in files):
        raise ValueError("that file is not part of this post")
    if len(files) < 2:
        raise ValueError("this is the post's only file: delete the post instead")
    gone = next(f for f in files if f["sha"] == sha)
    if files[0]["sha"] == sha:                      # promote the next file to be the post's main file
        nxt = files[1]
        other = c.execute("SELECT id FROM posts WHERE sha256=? AND id!=?", (nxt["sha"], pid)).fetchone()
        if other:
            raise ValueError(f"the next file is also post #{other[0]}'s main file: delete that one from here instead")
        c.execute("DELETE FROM post_media WHERE post_id=? AND sha256=?", (pid, nxt["sha"]))
        c.execute("UPDATE posts SET sha256=?, ext=?, mime=?, width=?, height=? WHERE id=?",
                  (nxt["sha"], nxt["ext"], nxt["mime"], nxt["w"], nxt["h"], pid))
    else:
        c.execute("DELETE FROM post_media WHERE post_id=? AND sha256=?", (pid, sha))
    c.execute("INSERT OR REPLACE INTO deleted_media(sha256, post_id) VALUES (?, ?)", (sha, pid))
    _remove_file_if_unused(c, Path(data) if data else DATA, sha, gone["ext"])


# ---------- settings and branding (each install picks its own name and icon) ----------
DEFAULT_NAME = "ScrapeArc"
ICON_FILES = ("icon-128.png", "icon-192.png", "apple-touch-icon.png", "favicon-64.png", "favicon.ico")


def get_setting(c, key: str, default=None):
    r = c.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return r[0] if r else default


def set_setting(c, key: str, value: str) -> None:
    c.execute("INSERT OR REPLACE INTO settings(key, value) VALUES (?, ?)", (key, value))


def upload_key(c, new: bool = False) -> str:
    """Secret that lets the iPhone Shortcut upload without logging in (only /upload accepts it)."""
    key = get_setting(c, "upload_key")
    if new or not key:
        import secrets
        key = secrets.token_urlsafe(24)
        set_setting(c, "upload_key", key)
    return key


def heic_to_jpeg(path: Path) -> Path:
    """iPhone photos (HEIC/HEIF) -> JPEG, since most browsers can't show HEIC. Other files are returned unchanged."""
    path = Path(path)
    if path.suffix.lower() not in (".heic", ".heif"):
        return path
    try:
        import pillow_heif
        from PIL import Image, ImageOps
        pillow_heif.register_heif_opener()
        with Image.open(path) as im:
            exif = im.getexif()
            im = ImageOps.exif_transpose(im).convert("RGB")   # turn the picture the way the phone held it
        if exif:
            exif[0x0112] = 1   # orientation is now applied
        out = path.with_suffix(".jpg")
        im.save(out, "JPEG", quality=92, exif=exif.tobytes() if exif else b"")
        return out
    except Exception as e:   # unreadable: let the normal checks reject it
        print(f"HEIC conversion failed: {type(e).__name__}", flush=True)
        return path


def clean_site_name(name: str) -> str:
    name = re.sub(r"\s+", " ", str(name or "")).strip()
    if not name:
        raise ValueError("the name can't be empty")
    if len(name) > 40:
        raise ValueError("the name can be at most 40 characters")
    return name


def make_icon_set(im, dest: Path) -> None:
    """Every icon size the app uses, from one picture (center-cropped to a square)."""
    from PIL import Image, ImageDraw, ImageOps
    im = ImageOps.exif_transpose(im).convert("RGB")
    side = min(im.size)
    im = im.crop(((im.width - side) // 2, (im.height - side) // 2, (im.width + side) // 2, (im.height + side) // 2))
    dest.mkdir(parents=True, exist_ok=True)

    def sized(n):
        return im.resize((n, n), Image.LANCZOS)

    def rounded(n, r):
        out = sized(n).convert("RGBA")
        m = Image.new("L", (n * 4, n * 4), 0)
        ImageDraw.Draw(m).rounded_rectangle([0, 0, n * 4 - 1, n * 4 - 1], r * 4, fill=255)
        out.putalpha(m.resize((n, n), Image.LANCZOS))
        return out

    tmp = {}
    tmp["icon-128.png"] = sized(128)              # header (rounded by CSS)
    tmp["icon-192.png"] = sized(192)
    tmp["apple-touch-icon.png"] = sized(180)      # iPhone rounds it itself
    tmp["favicon-64.png"] = rounded(64, 14)
    for name, img in tmp.items():
        img.save(dest / (name + ".tmp"), "PNG")
    rounded(64, 14).save(dest / "favicon.ico.tmp", "ICO", sizes=[(16, 16), (32, 32), (48, 48), (64, 64)])
    for name in ICON_FILES:
        (dest / (name + ".tmp")).replace(dest / name)


def branding_dir(data=None) -> Path:
    return (Path(data) if data else DATA) / "branding"


def set_icon(raw: bytes, data=None) -> None:
    from io import BytesIO
    from PIL import Image
    try:
        with Image.open(BytesIO(raw)) as im:
            im.load()
            make_icon_set(im, branding_dir(data))
    except (OSError, Image.DecompressionBombError) as e:
        raise ValueError(f"not a usable picture: {e}")


def reset_icon(data=None) -> None:
    for name in ICON_FILES:
        (branding_dir(data) / name).unlink(missing_ok=True)


def brand_info(c, data=None) -> dict:
    """{name, configured, icon_v}: icon_v is 0 for the built-in icon, else a version for cache busting."""
    name = get_setting(c, "site_name")
    custom = branding_dir(data) / "icon-128.png"
    try:   # how much the background is darkened by default (new installs: enough to keep text readable)
        dim = min(max(float(get_setting(c, "bg_dim", "0")), 0.0), 0.8)
    except ValueError:
        dim = 0.0
    iid = get_setting(c, "install_id")
    return {"name": name or DEFAULT_NAME, "configured": name is not None, "dim": dim,
            "theme_key": f"mb-theme-{iid}" if iid else "mb-theme",   # older installs keep their saved colours
            "icon_v": int(custom.stat().st_mtime) if custom.exists() else 0}


# ---------- backgrounds (one for desktop, one for phones; shared by every device) ----------
BG_SLOTS = ("desktop", "mobile")


def background_path(slot: str, data=None) -> Path:
    if slot not in BG_SLOTS:
        raise ValueError("background is 'desktop' or 'mobile'")
    return (Path(data) if data else DATA) / "backgrounds" / f"{slot}.jpg"


def set_background(slot: str, raw: bytes, data=None) -> None:
    """Store a background picture (re-encoded as JPEG, at most 2560px, so any odd upload ends up a plain image)."""
    from io import BytesIO
    from PIL import Image, ImageOps
    dest = background_path(slot, data)
    try:
        with Image.open(BytesIO(raw)) as im:
            im = ImageOps.exif_transpose(im).convert("RGB")
            im.thumbnail((2560, 2560))
            dest.parent.mkdir(parents=True, exist_ok=True)
            tmp = dest.with_suffix(".tmp")
            im.save(tmp, "JPEG", quality=85)
            tmp.replace(dest)
    except (OSError, Image.DecompressionBombError) as e:
        raise ValueError(f"not a usable picture: {e}")


NEW_INSTALL_DIM = "0.5"   # new installs fade a background picture you add this much, so text over it stays readable


def background_versions(data=None) -> dict:
    """{slot: version} for the backgrounds that are set (version changes when the picture does, for cache busting)."""
    out = {}
    for slot in BG_SLOTS:
        p = background_path(slot, data)
        if p.exists():
            out[slot] = int(p.stat().st_mtime)
    return out


# ---------- keyword matching (for filtering imports by title) ----------
def keyword_match(text: str, keywords) -> list[str]:
    """Case-insensitive. ASCII words need word boundaries; non-ASCII (e.g. Korean) match as substrings."""
    hits = []
    for kw in keywords:
        k = kw.strip().lower()
        if not k:
            continue
        if k.isascii():
            ok = re.search(rf"(?<!\w){re.escape(k)}(?!\w)", text.lower())
        else:
            ok = k in text.lower()
        if ok:
            hits.append(kw)
    return hits


# ---------- search ----------
# "view" filter on the post list: which kind of posts to show
VIEWS = {"all": "All posts", "videos": "Videos", "images": "Images", "fav": "Favorite",
         "fav_videos": "Favorite Videos", "fav_images": "Favorite Pics", "dups": "Possible duplicates",
         "bookmarked": "Bookmarked"}
BOOKMARK_SQL = "p.id IN (SELECT post_id FROM scraped WHERE status='bookmark')"   # saved with the Save bookmark
# second filter next to the view: everything / only things you uploaded / only scraped & imported (by the 'upload' tag)
SOURCES = {"all": "Everything", "upload": "Uploads", "scraped": "Not uploads"}
UPLOAD_TAG = "upload"
# both are worked out once per query (not once per post): with 30,000+ posts the per-post form took 14 s and 190 s a page
DUP_SQL = "p.id IN (SELECT a FROM dup_flags WHERE status='open' UNION ALL SELECT b FROM dup_flags WHERE status='open')"


def parse_query(q: str):
    inc, exc, flt = [], [], {}
    for tok in q.split():
        if tok.startswith("-") and len(tok) > 1:
            exc.append(tok[1:])
        elif ":" in tok and tok.split(":", 1)[0] in ("type", "title"):
            k, v = tok.split(":", 1)
            flt[k] = v
        else:
            inc.append(tok)
    return inc, exc, flt


SORTS = {"new": "COALESCE(p.posted, p.created) DESC, p.id DESC",
         "old": "COALESCE(p.posted, p.created) ASC, p.id ASC"}


def _like(text: str) -> str:
    """Text for a LIKE pattern, with its wildcards taken literally."""
    return text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def title_words(c, q: str) -> list[str]:
    """Words of a search that are not tags, so they were looked for in titles (shown above the results)."""
    inc, exc, _ = parse_query(q)
    return [w for w in inc if lookup_tag(c, w) is None] + ["-" + w for w in exc if lookup_tag(c, w) is None]


def _filters(c, q: str, view: str = "all", src: str = "all"):
    """WHERE clause + args for a search query and view (None only when the view can have no results)."""
    inc, exc, flt = parse_query(q)
    where, args = [], []
    # a word that is a tag searches that tag; any other word searches titles instead (see title_words)
    for name in inc:
        tid = lookup_tag(c, name)
        if tid is None:
            where.append("p.title LIKE ? ESCAPE '\\'")
            args.append(f"%{_like(name)}%")
            continue
        where.append("EXISTS(SELECT 1 FROM post_tags WHERE post_id=p.id AND tag_id=?)")
        args.append(tid)
    for name in exc:
        tid = lookup_tag(c, name)
        if tid is None:
            where.append("p.title NOT LIKE ? ESCAPE '\\'")
            args.append(f"%{_like(name)}%")
            continue
        where.append("NOT EXISTS(SELECT 1 FROM post_tags WHERE post_id=p.id AND tag_id=?)")
        args.append(tid)
    if flt.get("type") in ("image", "video"):
        where.append("p.mime LIKE ?")
        args.append(flt["type"] + "/%")
    if flt.get("title"):
        where.append("p.title LIKE ?")
        args.append(f"%{flt['title']}%")
    if view in ("videos", "fav_videos"):
        where.append("p.mime LIKE 'video/%'")
    elif view in ("images", "fav_images"):
        where.append("p.mime LIKE 'image/%'")
    if view in ("fav", "fav_videos", "fav_images"):
        where.append("p.favorite=1")
    if view == "dups":
        where.append(DUP_SQL)
    if view == "bookmarked":
        where.append(BOOKMARK_SQL)
    if src in ("upload", "scraped"):
        tid = lookup_tag(c, UPLOAD_TAG)
        if tid is None:
            if src == "upload":
                return None           # nothing uploaded yet
        else:
            where.append(("" if src == "upload" else "NOT ") + "EXISTS(SELECT 1 FROM post_tags WHERE post_id=p.id AND tag_id=?)")
            args.append(tid)
    return where, args


PER_PAGE = 48   # posts per page: fills whole rows at 1, 2, 3, 4, 6 and 8 per row


def search(c, q: str, page: int = 1, per: int = PER_PAGE, sort: str = "new", view: str = "all", src: str = "all"):
    """sort: 'new' / 'old' by the time posted on the original site (import time when the site gave none)."""
    f = _filters(c, q, view, src)
    if f is None:
        return [], 0
    where, args = f
    clause = (" WHERE " + " AND ".join(where)) if where else ""
    total = c.execute(f"SELECT COUNT(*) FROM posts p{clause}", args).fetchone()[0]
    rows = c.execute(f"SELECT p.*, {DUP_SQL} AS dup, 1 + (SELECT COUNT(*) FROM post_media m WHERE m.post_id=p.id) AS nfiles FROM posts p{clause} ORDER BY {SORTS.get(sort, SORTS['new'])} LIMIT ? OFFSET ?",
                     args + [per, (max(page, 1) - 1) * per]).fetchall()
    return rows, total


def neighbors(c, pid: int, q: str = "", sort: str = "new", view: str = "all", src: str = "all"):
    """(previous id, next id) of a post within the same list (query + view + sort) the grid shows; None at either end."""
    cur = c.execute("SELECT COALESCE(posted, created) AS k, id FROM posts WHERE id=?", (pid,)).fetchone()
    f = _filters(c, q, view, src)
    if cur is None or f is None:
        return None, None
    where, args = f
    key = "(COALESCE(p.posted, p.created), p.id)"
    def one(cmp, order):
        clause = " AND ".join(where + [f"{key} {cmp} (?, ?)"])
        r = c.execute(f"SELECT p.id FROM posts p WHERE {clause} ORDER BY COALESCE(p.posted, p.created) {order}, p.id {order} LIMIT 1",
                      args + [cur["k"], cur["id"]]).fetchone()
        return r["id"] if r else None
    newer, older = one(">", "ASC"), one("<", "DESC")
    return (older, newer) if sort == "old" else (newer, older)

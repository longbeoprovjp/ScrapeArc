"""Find pictures/videos that show the same content even when the files differ (resized, recompressed, re-encoded).

Idea: shrink the picture to 9x8 grey pixels and record, for each row, whether each pixel is brighter than its right
neighbour. That is 64 bits (a "difference hash"). Copies of the same picture give almost the same bits; unrelated
pictures differ in about half of them. The number of differing bits is the Hamming distance.
Videos: the same fingerprint of a few frames spread over the clip.
Matches are only ever FLAGGED for you to review, never deleted automatically.
"""
import shutil
import subprocess
from pathlib import Path

IMG_MAX_DIST = 5        # of 64 bits: pictures this close count as the same
VID_FRAME_DIST = 8      # per video frame (video compression moves more bits)
VID_FRAMES = 5
VID_MIN_FRAMES = 3      # informative frame pairs needed before two videos can match
VID_MIN_RATIO = 0.8     # share of those pairs that must match


def dhash(gray72: bytes) -> int:
    """64-bit difference hash from 9x8 grey pixel values."""
    bits = 0
    for r in range(8):
        for c in range(8):
            bits = (bits << 1) | (gray72[r * 9 + c] > gray72[r * 9 + c + 1])
    return bits


def informative(h: int) -> bool:
    """Flat/black/white images hash to almost all 0s or 1s and would 'match' everything; ignore those."""
    return 10 <= h.bit_count() <= 54


def image_hashes(path: Path) -> list[int]:
    from PIL import Image, ImageOps
    with Image.open(path) as im:
        im = ImageOps.exif_transpose(im)          # phone photos store rotation in EXIF; web copies have it applied
        g = im.convert("L").resize((9, 8), Image.LANCZOS)
        return [dhash(bytes(g.tobytes()))]


def video_hashes(path: Path) -> list[int]:
    if not (shutil.which("ffmpeg") and shutil.which("ffprobe")):
        return []
    try:
        dur = float(subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(path)],
                                   capture_output=True, text=True, timeout=30).stdout.strip())
    except (ValueError, subprocess.SubprocessError):
        return []
    out = []
    for i in range(VID_FRAMES):
        t = dur * (i + 1) / (VID_FRAMES + 1)
        try:
            raw = subprocess.run(["ffmpeg", "-v", "error", "-ss", f"{t:.2f}", "-i", str(path), "-frames:v", "1",
                                  "-vf", "scale=9:8:flags=area,format=gray", "-f", "rawvideo", "-"],
                                 capture_output=True, timeout=60, stdin=subprocess.DEVNULL).stdout
        except subprocess.SubprocessError:
            continue
        out.append(dhash(raw) if len(raw) == 72 else 0)
    return out


def fingerprint(path: Path, mime: str):
    """('image'|'video', [hashes]) or None when it cannot be done."""
    try:
        if mime.startswith("image/"):
            return "image", image_hashes(path)
        if mime.startswith("video/"):
            hs = video_hashes(path)
            return ("video", hs) if hs else None
    except Exception:
        return None
    return None


def same_content(kind: str, a: list[int], b: list[int]):
    """Distance (int) when a and b look like the same content, else None."""
    if kind == "image":
        if not (a and b and informative(a[0]) and informative(b[0])):
            return None
        d = (a[0] ^ b[0]).bit_count()
        return d if d <= IMG_MAX_DIST else None
    pairs = [(x, y) for x, y in zip(a, b) if informative(x) and informative(y)]
    if len(pairs) < VID_MIN_FRAMES:
        return None
    dists = [(x ^ y).bit_count() for x, y in pairs]
    good = sum(d <= VID_FRAME_DIST for d in dists)
    return round(sum(dists) / len(dists)) if good / len(pairs) >= VID_MIN_RATIO else None


def _pack(hs):
    return " ".join(f"{h:016x}" for h in hs)


def _unpack(s):
    return [int(x, 16) for x in s.split()]


def ensure_fingerprint(c, base: Path, sha: str, ext: str, mime: str):
    """Fingerprint of a stored file, computing and saving it if needed. None if it has none."""
    row = c.execute("SELECT kind, hashes FROM fingerprints WHERE sha256=?", (sha,)).fetchone()
    if row:
        return (row[0], _unpack(row[1])) if row[0] != "none" else None
    fp = fingerprint(base / "files" / sha[:2] / f"{sha}.{ext}", mime)
    c.execute("INSERT OR REPLACE INTO fingerprints VALUES (?,?,?)", (sha, fp[0] if fp else "none", _pack(fp[1]) if fp else ""))
    return fp


def check_file(c, post_id: int, base: Path, sha: str, ext: str, mime: str) -> int:
    """Fingerprint one file of post_id and flag every OTHER post that holds the same content. Returns flags added.
    Never raises: a failure here must not stop an import."""
    try:
        fp = ensure_fingerprint(c, base, sha, ext, mime)
        if not fp:
            return 0
        kind, hs = fp
        added = 0
        for osha, ohashes in c.execute("SELECT sha256, hashes FROM fingerprints WHERE kind=? AND sha256!=?", (kind, sha)).fetchall():
            d = same_content(kind, hs, _unpack(ohashes))
            if d is None:
                continue
            for (other,) in c.execute("SELECT id FROM posts WHERE sha256=? UNION SELECT post_id FROM post_media WHERE sha256=?",
                                      (osha, osha)).fetchall():
                if other == post_id:
                    continue
                a, b = sorted((post_id, other))
                added += c.execute("INSERT OR IGNORE INTO dup_flags(a, b, dist) VALUES (?,?,?)", (a, b, d)).rowcount
        return added
    except Exception:
        return 0


def backfill(connect, base: Path, log=lambda m: None) -> int:
    """Fingerprint every stored file that has none yet (older posts) and flag matches. Safe to re-run."""
    done = 0
    with connect() as c:
        todo = c.execute("""SELECT id, sha256, ext, mime FROM posts WHERE sha256 NOT IN (SELECT sha256 FROM fingerprints)
                            UNION ALL SELECT post_id, sha256, ext, mime FROM post_media
                            WHERE sha256 NOT IN (SELECT sha256 FROM fingerprints)""").fetchall()
    for pid, sha, ext, mime in todo:
        with connect() as c:
            check_file(c, pid, base, sha, ext, mime)
        done += 1
    if done:
        log(f"duplicate check: fingerprinted {done} existing file(s)")
    return done


# ---------- copies of one picture inside a single post ----------
# Sites often put the same picture in a post twice (a full-size copy plus a thumbnail strip, or a "view larger" link).
# Inside one post we keep only the highest-resolution copy. Strict on purpose: two shots that differ only by a blink
# or a hand must both stay. So:
#   - the coarse 64-bit hash, the shape and the overall pixel difference must all agree, AND no single patch of the
#     picture may differ (a blink is a tiny patch: invisible in an average, obvious patch by patch);
#   - files are compared biggest first, and a file is dropped only as a smaller copy of a picture already kept.
#     Two full-size shots are compared with each other directly, never linked through a thumbnail that is too
#     small to show the difference.
# Calibrated on real posts (400 known full-size/thumbnail pairs, with a fake "blink" painted on the full-size one):
# with these values 84% of true copies are recognized, a blink 2% of the picture's width is always caught, a tiny
# 1% one 80-90% of the time. Raising COPY_MAX_PATCH to 12 recognizes 99% of copies but catches only ~half the tiny ones.
COPY_SIDE = 1024        # compare at the smaller file's size, at most this many pixels on the long side
COPY_CELL = 8           # patch size in pixels at that comparison size
COPY_BLUR = 1.5         # light smoothing first, so resize/recompression noise is not mistaken for a change
COPY_MAX_DIFF = 2.0     # mean absolute grey difference (0-255) over the whole picture
COPY_MAX_PATCH = 8.0    # ... and in the worst single patch. Lower = stricter (more near-identical shots kept)
COPY_MAX_ASPECT = 0.02  # width/height ratios within 2% (a crop is not a copy). This is the shape, not the similarity


def _copy_info(path: Path):
    """{hash, w, h, img (grey, at most COPY_SIDE), size} of a picture, or None (video, unreadable)."""
    import mimetypes
    from PIL import Image, ImageOps
    if not (mimetypes.guess_type(path.name)[0] or "").startswith("image/"):
        return None
    try:
        with Image.open(path) as im:
            im = ImageOps.exif_transpose(im).convert("L")
            w, h = im.size
            small = im.copy()
            small.thumbnail((COPY_SIDE, COPY_SIDE), Image.BOX)
            return {"hash": dhash(bytes(im.resize((9, 8), Image.LANCZOS).tobytes())), "w": w, "h": h, "img": small,
                    "size": path.stat().st_size}
    except Exception:
        return None


def copy_diff(a, b):
    """(mean difference, worst patch difference) of two pictures at the smaller one's resolution, or None when
    they are clearly not the same picture (hash or shape differ)."""
    from PIL import Image, ImageChops, ImageFilter, ImageStat
    if a is None or b is None or not (informative(a["hash"]) and informative(b["hash"])):
        return None
    if (a["hash"] ^ b["hash"]).bit_count() > IMG_MAX_DIST:
        return None
    ra, rb = a["w"] / a["h"], b["w"] / b["h"]
    if abs(ra - rb) / max(ra, rb) > COPY_MAX_ASPECT:
        return None
    small = a if a["w"] * a["h"] <= b["w"] * b["h"] else b
    scale = min(1.0, COPY_SIDE / max(small["w"], small["h"]))
    size = (max(8, round(small["w"] * scale)), max(8, round(small["h"] * scale)))
    x, y = (im.resize(size, Image.BOX).filter(ImageFilter.GaussianBlur(COPY_BLUR)) for im in (a["img"], b["img"]))
    d = ImageChops.difference(x, y)
    mean = ImageStat.Stat(d).mean[0]
    patches = d.resize((max(1, round(size[0] / COPY_CELL)), max(1, round(size[1] / COPY_CELL))), Image.BOX)
    return mean, float(patches.getextrema()[1])


def is_copy(a, b) -> bool:
    r = copy_diff(a, b)
    return r is not None and r[0] <= COPY_MAX_DIFF and r[1] <= COPY_MAX_PATCH


def drop_copies(paths: list[Path]) -> tuple[list[Path], int]:
    """Files of one post -> (files to keep, number dropped). Biggest first: a file is dropped only when it is a
    copy of a file already kept. The kept copy takes the place where that picture first appeared. Videos and
    unreadable files are always kept."""
    info = [_copy_info(p) for p in paths]
    order = sorted(range(len(paths)), key=lambda i: (-(info[i]["w"] * info[i]["h"]) if info[i] else 0,
                                                     -(info[i]["size"]) if info[i] else 0, i))
    kept: list[int] = []
    first_pos: dict[int, int] = {}
    for i in order:
        twin = next((k for k in kept if is_copy(info[k], info[i])), None)
        if twin is None:
            kept.append(i)
            first_pos[i] = i
        else:
            first_pos[twin] = min(first_pos[twin], i)
    kept.sort(key=lambda k: first_pos[k])
    return [paths[k] for k in kept], len(paths) - len(kept)

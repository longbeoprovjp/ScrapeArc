"""Copies of one picture inside a post: keep only the highest-resolution one, never merge different shots."""
import random

from PIL import Image, ImageDraw

from minibooru import core, similar


def scene(seed, size=(600, 400), shift=0):
    """A picture with structure (random shapes on a gradient), so its fingerprint is informative."""
    rnd = random.Random(seed)
    im = Image.linear_gradient("L").resize(size).convert("RGB")
    d = ImageDraw.Draw(im)
    for _ in range(14):
        x, y = rnd.randrange(size[0]), rnd.randrange(size[1])
        r = rnd.randrange(20, 120)
        d.ellipse([x - r + shift, y - r, x + r + shift, y + r], fill=tuple(rnd.randrange(256) for _ in range(3)))
    return im


def save(im, path, **kw):
    im.save(path, **kw)
    return path


def test_resized_copy_is_dropped_keeping_the_bigger(tmp_path):
    big = save(scene(1), tmp_path / "big.jpg", quality=92)
    other = save(scene(2), tmp_path / "other.jpg")
    thumb = save(scene(1).resize((150, 100)), tmp_path / "thumb.jpg", quality=70)
    kept, dropped = similar.drop_copies([thumb, other, big])
    assert dropped == 1 and kept == [big, other]       # the best copy takes the place the picture first appeared


def test_different_shots_and_crops_are_kept(tmp_path):
    a = save(scene(3), tmp_path / "a.jpg")
    burst = save(scene(3, shift=5), tmp_path / "b.jpg")              # subject moved a little: hash is identical, pixels are not
    crop = save(scene(3).crop((0, 0, 400, 400)), tmp_path / "c.jpg")  # a crop is a different picture
    kept, dropped = similar.drop_copies([a, burst, crop])
    assert dropped == 0 and kept == [a, burst, crop]


def test_videos_and_unreadable_files_pass_through(tmp_path):
    v = tmp_path / "clip.mp4"
    v.write_bytes(b"not really a video")
    bad = tmp_path / "bad.jpg"
    bad.write_bytes(b"junk")
    assert similar.drop_copies([v, bad]) == ([v, bad], 0)


def test_collapse_copies_on_a_saved_post(tmp_path):
    data = tmp_path / "d"
    thumb = save(scene(4).resize((120, 80)), tmp_path / "t.jpg")
    big = save(scene(4), tmp_path / "big.jpg")
    other = save(scene(5), tmp_path / "o.jpg")
    with core.connect(data) as c:
        pid, _ = core.ingest(c, thumb, ["x"], data=data)          # an old import: the thumbnail became the main file
        core.add_media(c, pid, other, data=data)
        core.add_media(c, pid, big, data=data)
        thumb_sha = core.post_files(c, pid)[0]["sha"]
        assert core.collapse_copies(c, pid, data=data, apply=False) == 1 and len(core.post_files(c, pid)) == 3
        assert core.collapse_copies(c, pid, data=data) == 1
        files = core.post_files(c, pid)
        assert [(f["w"], f["h"]) for f in files] == [(600, 400), (600, 400)]
        assert thumb_sha not in {f["sha"] for f in files}
        assert not list((data / "files").rglob(f"{thumb_sha}.*")) and not (data / "thumbs" / f"{thumb_sha}.jpg").exists()
        assert core.collapse_copies(c, pid, data=data) == 0       # safe to run again


def blinked(im, frac=0.02):
    """Same shot with one small patch changed (like an eye closing): 2% of the width."""
    im = im.copy()
    w, h = im.size
    ew = int(w * frac)
    ImageDraw.Draw(im).ellipse([w // 2, h // 3, w // 2 + ew, h // 3 + ew // 2], fill=(20, 20, 20))
    return im


def test_blink_shots_are_both_kept(tmp_path):
    a = save(scene(6), tmp_path / "open.jpg", quality=92)
    b = save(blinked(scene(6)), tmp_path / "blink.jpg", quality=92)
    assert (similar._copy_info(a)["hash"] ^ similar._copy_info(b)["hash"]).bit_count() <= similar.IMG_MAX_DIST  # hash alone can't tell
    assert similar.drop_copies([a, b]) == ([a, b], 0)


def test_thumbnails_do_not_link_two_different_shots(tmp_path):
    """Open-eyes and blink shots, each with a thumbnail: both full-size shots stay, both thumbnails go."""
    a = save(scene(7), tmp_path / "a.jpg", quality=92)
    b = save(blinked(scene(7)), tmp_path / "b.jpg", quality=92)
    ta = save(scene(7).resize((300, 200), Image.LANCZOS), tmp_path / "ta.jpg", quality=90)
    tb = save(blinked(scene(7)).resize((300, 200), Image.LANCZOS), tmp_path / "tb.jpg", quality=90)
    kept, dropped = similar.drop_copies([a, b, ta, tb])
    assert kept == [a, b] and dropped == 2

"""Clearing near-certain duplicates, and the upload tag never moving onto a post that wasn't uploaded."""
import pytest
from fastapi.testclient import TestClient
from PIL import Image

from minibooru import core


@pytest.fixture
def data(tmp_path, monkeypatch):
    monkeypatch.setattr(core, "DATA", tmp_path / "data")
    monkeypatch.delenv("MINIBOORU_PASSWORD", raising=False)
    return tmp_path


def post(c, tmp, name, size, color, tags, source=""):
    p = tmp / f"{name}.png"
    Image.new("RGB", size, color).save(p)
    if source == "upload":
        tags = list(tags) + [core.UPLOAD_TAG]
    return core.ingest(c, p, tags, title=name, source=source)[0]


def tags(c, pid):
    return sorted(r["name"] for r in core.post_tags(c, pid))


def flag(c, a, b, dist):
    a, b = sorted((a, b))
    c.execute("INSERT INTO dup_flags(a, b, dist) VALUES (?,?,?)", (a, b, dist))


def test_resolve_keeps_upload_tag_off_scraped_posts(data):
    with core.connect() as c:
        scraped = post(c, data, "s", (80, 60), "red", ["cat"], source="https://example.com/p/1")
        c.execute("INSERT INTO scraped(post_url, post_id, status) VALUES ('https://example.com/p/1', ?, 'new')", (scraped,))
        up = post(c, data, "u", (40, 30), "blue", ["fluffy"], source="upload")
        c.execute("UPDATE posts SET favorite=1 WHERE id=?", (up,))
        up_sha = c.execute("SELECT sha256 FROM posts WHERE id=?", (up,)).fetchone()[0]
        assert core.resolve_duplicate(c, scraped, up)
        assert tags(c, scraped) == ["cat", "fluffy"]                       # tags merged, 'upload' not
        assert c.execute("SELECT favorite FROM posts WHERE id=?", (scraped,)).fetchone()[0] == 1
        assert c.execute("SELECT post_id FROM scraped").fetchone()[0] == scraped   # scrape record kept
        assert core.is_deleted_file(c, up_sha)                             # a rescan won't save the copy again
        assert not c.execute("SELECT 1 FROM posts WHERE id=?", (up,)).fetchone()
        assert not core.resolve_duplicate(c, scraped, up)                  # already gone
        # keeping the upload: it stays an upload
        up2 = post(c, data, "u2", (90, 60), "green", ["x"], source="upload")
        sc2 = post(c, data, "s2", (30, 20), "yellow", ["y"], source="https://example.com/p/2")
        assert core.resolve_duplicate(c, up2, sc2)
        assert tags(c, up2) == [core.UPLOAD_TAG, "x", "y"]


def test_merge_into_one_drops_upload_tag(data):
    with core.connect() as c:
        scraped = post(c, data, "s", (80, 60), "red", ["cat"], source="https://example.com/p/1")
        up = post(c, data, "u", (40, 30), "blue", ["dog"], source="upload")
        assert core.merge_posts(c, [scraped, up]) == scraped
        assert tags(c, scraped) == ["cat", "dog"]


def test_sure_duplicates_picks_certain_pairs_and_the_better_copy(data):
    with core.connect() as c:
        big = post(c, data, "big", (400, 300), "red", ["a"])
        small = post(c, data, "small", (200, 150), "pink", ["b"])
        flag(c, big, small, 1)                                   # sure: same shape, close -> keep the bigger
        far1, far2 = post(c, data, "f1", (100, 100), "navy", []), post(c, data, "f2", (100, 100), "teal", [])
        flag(c, far1, far2, 4)                                   # too far apart: by hand
        w1, w2 = post(c, data, "w1", (300, 100), "olive", []), post(c, data, "w2", (100, 100), "lime", [])
        flag(c, w1, w2, 0)                                       # different shape: by hand
        m1, m2 = post(c, data, "m1", (100, 100), "gray", []), post(c, data, "m2", (100, 100), "black", [])
        Image.new("RGB", (100, 100), "white").save(data / "extra.png")
        core.add_media(c, m1, data / "extra.png")
        flag(c, m1, m2, 0)                                       # multi-file post: by hand
        c.execute("UPDATE dup_flags SET status='dismissed' WHERE a=? AND b=?", tuple(sorted((far1, far2))))
        flag(c, far1, w2, 2)                                     # both square, 2 bits apart: sure
        pairs = core.sure_duplicates(c)
        assert pairs[0] == (big, small)                          # closest first; the bigger picture is kept
        assert [set(p) for p in pairs] == [{big, small}, {far1, w2}]   # nothing else qualifies


def test_sure_page_and_clear(data):
    from minibooru.web import create_app
    with core.connect() as c:
        big = post(c, data, "big", (400, 300), "red", ["a"])
        small = post(c, data, "small", (200, 150), "pink", ["b"])
        flag(c, big, small, 0)
    cl = TestClient(create_app())
    r = cl.get("/dups/sure")
    assert r.status_code == 200 and "Clear all 1 sure duplicates" in r.text
    assert cl.post("/dups/sure", data={"expect": 0}, follow_redirects=False).headers["location"] == "/dups/sure"  # more than seen: nothing done
    with core.connect() as c:
        assert c.execute("SELECT COUNT(*) FROM posts").fetchone()[0] == 2
    r = cl.post("/dups/sure", data={"expect": 1}, follow_redirects=False)
    assert r.headers["location"] == "/dups/sure?done=1"
    with core.connect() as c:
        assert [r[0] for r in c.execute("SELECT id FROM posts")] == [big]
        assert tags(c, big) == ["a", "b"]
    assert "Cleared 1 duplicate" in cl.get("/dups/sure?done=1").text

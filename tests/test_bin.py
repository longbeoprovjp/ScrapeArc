"""The Bin: deleted posts can be restored for TRASH_DAYS; and the low-space warning."""
import pytest
from fastapi.testclient import TestClient
from PIL import Image

from minibooru import core


@pytest.fixture
def data(tmp_path, monkeypatch):
    monkeypatch.setattr(core, "DATA", tmp_path / "data")
    monkeypatch.delenv("MINIBOORU_PASSWORD", raising=False)
    return tmp_path


def make(c, tmp, name, color, tags=(), source=""):
    Image.new("RGB", (60, 40), color).save(tmp / f"{name}.png")
    return core.ingest(c, tmp / f"{name}.png", list(tags), title=name, source=source)[0]


def stored(sha, ext="png"):
    return core.DATA / "files" / sha[:2] / f"{sha}.{ext}"


def test_delete_then_restore(data):
    with core.connect() as c:
        pid = make(c, data, "a", "red", ["robin", "stage"], source="https://example.com/p/1")
        Image.new("RGB", (30, 30), "blue").save(data / "extra.png")
        core.add_media(c, pid, data / "extra.png")
        c.execute("UPDATE posts SET favorite=1 WHERE id=?", (pid,))
        c.execute("INSERT INTO scraped(post_url, post_id, status) VALUES ('https://example.com/p/1', ?, 'new')", (pid,))
        shas = [f["sha"] for f in core.post_files(c, pid)]
        assert core.delete_post(c, pid)
        assert not c.execute("SELECT 1 FROM posts WHERE id=?", (pid,)).fetchone()
        assert not stored(shas[0]).exists() and (core.DATA / "trash" / "files" / shas[0][:2] / f"{shas[0]}.png").exists()
        tid = c.execute("SELECT id FROM trash").fetchone()[0]
        back = core.restore_post(c, tid)
        assert back == pid                                              # same number again
        assert [f["sha"] for f in core.post_files(c, pid)] == shas and stored(shas[0]).exists()
        assert sorted(r["name"] for r in core.post_tags(c, pid)) == ["robin", "stage"]
        assert c.execute("SELECT favorite FROM posts WHERE id=?", (pid,)).fetchone()[0] == 1
        assert c.execute("SELECT post_id FROM scraped").fetchone()[0] == pid
        assert c.execute("SELECT COUNT(*) FROM trash").fetchone()[0] == 0
        assert (core.DATA / "thumbs" / f"{shas[0]}.jpg").exists()


def test_restore_refuses_when_the_picture_is_back(data):
    with core.connect() as c:
        pid = make(c, data, "a", "red")
        core.delete_post(c, pid)
        again = make(c, data, "a", "red")                               # uploaded again meanwhile
        with pytest.raises(ValueError, match=f"#{again}"):
            core.restore_post(c, c.execute("SELECT id FROM trash").fetchone()[0])


def test_purge_old_and_shared_files(data):
    with core.connect() as c:
        a, b = make(c, data, "a", "red"), make(c, data, "b", "green")
        sha = c.execute("SELECT sha256 FROM posts WHERE id=?", (a,)).fetchone()[0]
        core.delete_post(c, a)
        core.delete_post(c, b)
        c.execute("UPDATE trash SET deleted=datetime('now', '-31 days') WHERE post_id=?", (a,))
        assert core.purge_trash(c) == 1                                  # only the old one
        assert not (core.DATA / "trash" / "files" / sha[:2] / f"{sha}.png").exists()
        assert c.execute("SELECT post_id FROM trash").fetchall()[0][0] == b
        assert core.purge_trash(c, days=None) == 1                       # Empty the Bin
        assert not any((core.DATA / "trash" / "files").rglob("*.png"))


def test_resolved_duplicate_can_be_restored(data):
    with core.connect() as c:
        keep = make(c, data, "k", "red", ["x"], source="https://example.com/p/1")
        drop = make(c, data, "d", "navy", [core.UPLOAD_TAG], source="upload")
        dsha = c.execute("SELECT sha256 FROM posts WHERE id=?", (drop,)).fetchone()[0]
        core.resolve_duplicate(c, keep, drop)
        assert core.is_deleted_file(c, dsha)
        core.restore_post(c, c.execute("SELECT id FROM trash").fetchone()[0])
        assert not core.is_deleted_file(c, dsha)                         # wanted again
        assert [r["name"] for r in core.post_tags(c, drop)] == [core.UPLOAD_TAG]


def test_bin_pages_and_low_space(data, monkeypatch):
    from minibooru import web
    with core.connect() as c:
        pid = make(c, data, "a", "red", ["cat"])
        sha = c.execute("SELECT sha256 FROM posts WHERE id=?", (pid,)).fetchone()[0]
    cl = TestClient(web.create_app())
    cl.post("/posts/bulk-delete", data={"ids": [pid], "next": "/"})
    r = cl.get("/bin")
    assert r.status_code == 200 and f"#{pid}" in r.text and "Restore" in r.text
    assert cl.get(f"/bin/thumbs/{sha}.jpg").status_code == 200
    assert cl.get("/bin/thumbs/../db.sqlite").status_code == 404
    with core.connect() as c:
        tid = c.execute("SELECT id FROM trash").fetchone()[0]
    r = cl.post(f"/bin/{tid}/restore", follow_redirects=False)
    assert r.headers["location"] == f"/post/{pid}"
    assert "in the Bin" in cl.get("/stats").text
    monkeypatch.setattr(web, "LOW_SPACE", 0)                             # plenty of space: no warning
    assert "Running low on space" not in cl.get("/stats").text
    monkeypatch.setattr(web, "LOW_SPACE", 1 << 62)                       # pretend the drive is nearly full
    assert "Running low on space" in cl.get("/stats").text and "free on the archive" in cl.get("/").text
    cl.post(f"/post/{pid}/delete", data={"next": "/"})
    assert "Emptied the Bin: 1 post" in cl.post("/bin/empty").text

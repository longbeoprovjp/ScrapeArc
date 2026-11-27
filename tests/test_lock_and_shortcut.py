"""Reboot safety (data marker) and the iPhone Shortcut upload key."""
import pytest
from fastapi.testclient import TestClient
from PIL import Image

from minibooru import core


@pytest.fixture
def data(tmp_path, monkeypatch):
    monkeypatch.setattr(core, "DATA", tmp_path / "data")
    return tmp_path / "data"


def test_locked_folder_is_left_alone(data, monkeypatch):
    from minibooru.web import create_app
    monkeypatch.setenv("MINIBOORU_DATA_REQUIRED", "1")
    data.mkdir()                                   # the bare mount folder: empty
    cl = TestClient(create_app())
    for path in ("/", "/login", "/upload"):
        r = cl.get(path, follow_redirects=False)
        assert r.status_code == 503 and "data is locked" in r.text
    assert cl.post("/upload").status_code == 503
    assert list(data.iterdir()) == []              # no database, no folders, no session key


def test_missing_folder_is_not_created(data, monkeypatch):
    monkeypatch.setenv("MINIBOORU_DATA_REQUIRED", "1")
    assert not core.data_ready() and not data.exists()


def test_existing_archive_gets_marker(data, monkeypatch, tmp_path):
    with core.connect() as c:                      # an archive from before the marker existed
        Image.new("RGB", (8, 8), "red").save(tmp_path / "a.png")
        core.ingest(c, tmp_path / "a.png")
    monkeypatch.setenv("MINIBOORU_DATA_REQUIRED", "1")
    assert core.data_ready() and (data / core.DATA_MARKER).is_file()
    (data / "db.sqlite").unlink()                  # marker alone is enough from now on
    assert core.data_ready()


def test_empty_database_without_marker_is_locked(data, monkeypatch):
    with core.connect():
        pass
    monkeypatch.setenv("MINIBOORU_DATA_REQUIRED", "1")
    assert not core.data_ready()


def test_new_install_without_setting(data, monkeypatch):
    monkeypatch.delenv("MINIBOORU_DATA_REQUIRED", raising=False)
    assert core.data_ready() and (data / core.DATA_MARKER).is_file()


def test_shortcut_upload_key(data, monkeypatch, tmp_path):
    from minibooru.web import _fails, create_app
    _fails.clear()
    monkeypatch.setenv("MINIBOORU_PASSWORD", "pw")
    cl = TestClient(create_app())
    png = tmp_path / "s.png"
    Image.new("RGB", (20, 20), "blue").save(png)
    send = lambda key, **kw: cl.post("/upload", headers={"X-Upload-Key": key},
                                     files={"files": ("s.png", png.read_bytes(), "image/png")}, **kw)
    assert send("anything").status_code == 401                      # no key made yet
    cl.post("/login", data={"password": "pw"})
    page = cl.get("/upload/shortcut")
    assert page.status_code == 200
    with core.connect() as c:
        key = core.get_setting(c, "upload_key")
    assert key and key in page.text
    cl.cookies.clear()                                               # the Shortcut has no login
    assert send("wrong").status_code == 401
    r = send(key, data={"tags": "phone"})
    assert r.status_code == 200 and r.text == "1 new"
    assert send(key).text == "1 already in the archive"
    with core.connect() as c:
        assert c.execute("SELECT COUNT(*) FROM post_tags pt JOIN tags t ON t.id=pt.tag_id WHERE t.name='phone'").fetchone()[0] == 1
    # the key opens nothing else
    assert cl.get("/", headers={"X-Upload-Key": key}, follow_redirects=False).status_code == 303
    assert cl.post("/posts/bulk-delete", headers={"X-Upload-Key": key}, data={"ids": [1]}).status_code == 401
    # a new key replaces the old one
    cl.post("/login", data={"password": "pw"})
    cl.post("/upload/shortcut/new-key")
    cl.cookies.clear()
    assert send(key).status_code == 401
    _fails.clear()


def test_wrong_keys_are_rate_limited(data, monkeypatch):
    from minibooru.web import _fails, create_app
    _fails.clear()
    monkeypatch.setenv("MINIBOORU_PASSWORD", "pw")
    with core.connect() as c:
        core.upload_key(c)
    cl = TestClient(create_app())
    codes = [cl.post("/upload", headers={"X-Upload-Key": "x"}).status_code for _ in range(6)]
    assert codes[:5] == [401] * 5 and codes[5] == 429
    _fails.clear()


def test_heic_becomes_jpeg(data, tmp_path):
    import pillow_heif
    pillow_heif.register_heif_opener()
    src = tmp_path / "IMG_0001.HEIC"
    Image.new("RGB", (30, 20), "green").save(src, format="HEIF")
    out = core.heic_to_jpeg(src)
    assert out.suffix == ".jpg"
    with Image.open(out) as im:
        assert im.format == "JPEG" and im.size == (30, 20)
    with core.connect() as c:
        pid, status = core.ingest(c, out)
    assert status == "new"


def test_bulk_tags(data, monkeypatch, tmp_path):
    from minibooru.web import create_app
    monkeypatch.delenv("MINIBOORU_PASSWORD", raising=False)
    with core.connect() as c:
        ids = []
        for i, col in enumerate(("red", "green", "blue")):
            Image.new("RGB", (9, 9), col).save(tmp_path / f"{i}.png")
            ids.append(core.ingest(c, tmp_path / f"{i}.png", ["old", "keep"])[0])
        core.add_alias(c, "ancient", "old")
        core.add_implication(c, "cat", "animal")
    cl = TestClient(create_app())
    r = cl.post("/posts/bulk-tags", data={"ids": ids[:2], "add": "cat new", "remove": "ancient", "next": "/?page=2"},
                follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/?page=2"
    with core.connect() as c:
        tags = lambda pid: sorted(r[0] for r in c.execute(
            "SELECT t.name FROM post_tags pt JOIN tags t ON t.id=pt.tag_id WHERE pt.post_id=?", (pid,)))
        assert tags(ids[0]) == tags(ids[1]) == ["animal", "cat", "keep", "new"]   # alias removed 'old', implication added
        assert tags(ids[2]) == ["keep", "old"]                                     # not selected: untouched
        assert core.bulk_tags(c, [ids[2], 999], remove=["nosuchtag"]) == (1, 0)
    assert cl.post("/posts/bulk-tags", data={"ids": ids, "next": "/"}).status_code == 400          # nothing typed
    assert cl.post("/posts/bulk-tags", data={"add": "x", "next": "/"}).status_code == 400          # nothing picked
    assert "Edit tags" in cl.get("/").text

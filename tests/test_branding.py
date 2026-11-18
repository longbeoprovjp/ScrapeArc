"""Each install picks its own name and icon; nothing personal is built in."""
from io import BytesIO

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from minibooru import core


def png(color="teal", size=(300, 200)):
    b = BytesIO()
    Image.new("RGB", size, color).save(b, "PNG")
    return b.getvalue()


@pytest.fixture
def app_client(tmp_path, monkeypatch):
    monkeypatch.setattr(core, "DATA", tmp_path / "d")
    monkeypatch.delenv("MINIBOORU_SKIP_SETUP", raising=False)
    monkeypatch.delenv("MINIBOORU_PASSWORD", raising=False)
    from minibooru.web import create_app
    return lambda: TestClient(create_app())


def test_first_run_asks_for_a_name_then_never_again(app_client):
    cl = app_client()
    r = cl.get("/tags", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/setup?next=%2Ftags"
    page = cl.get("/setup").text
    assert 'value="ScrapeArc"' in page                                     # the default name
    assert cl.get("/favicon.ico").status_code == 200                        # icons never redirect
    r = cl.post("/setup", data={"name": "  My   Archive ", "next": "/tags"}, follow_redirects=False)
    assert r.headers["location"] == "/tags"
    page = cl.get("/").text
    assert "<title>My Archive</title>" in page and ">My Archive</span></a>" in page
    assert cl.get("/tags", follow_redirects=False).status_code == 200      # no more setup
    assert app_client().get("/", follow_redirects=False).status_code == 200  # also after a restart


def test_setup_keeps_default_name_and_takes_an_icon(app_client):
    cl = app_client()
    cl.post("/setup", data={"name": "", "next": "/"}, files={"icon": ("i.png", png(), "image/png")})
    with core.connect() as c:
        info = core.brand_info(c)
    assert info["name"] == "ScrapeArc" and info["configured"] and info["icon_v"]
    got = cl.get("/brand/icon-128.png")
    assert got.status_code == 200 and Image.open(BytesIO(got.content)).size == (128, 128)
    assert f'/brand/icon-128.png?v={info["icon_v"]}' in cl.get("/").text


def test_customize_changes_name_and_icon(app_client):
    cl = app_client()
    cl.post("/setup", data={"name": "First", "next": "/"})
    default = cl.get("/brand/favicon-64.png").content
    assert cl.post("/settings/name", data={"name": "Second"}).json()["name"] == "Second"
    assert ">Second</span></a>" in cl.get("/").text and "<h1>Second</h1>" in cl.get("/login").text
    assert cl.post("/settings/name", data={"name": "   "}).status_code == 400
    assert cl.post("/settings/name", data={"name": "x" * 41}).status_code == 400
    assert cl.post("/settings/icon", files={"file": ("i.png", png("orange"), "image/png")}).json()["icon_v"]
    assert cl.get("/brand/favicon-64.png").content != default
    assert cl.post("/settings/icon", files={"file": ("i.png", b"nope", "image/png")}).status_code == 400
    assert cl.post("/settings/icon/reset").json()["icon_v"] == 0
    assert cl.get("/brand/favicon-64.png").content == default
    assert cl.get("/brand/secret.txt").status_code == 404


def test_password_login_comes_before_setup(tmp_path, monkeypatch):
    monkeypatch.setattr(core, "DATA", tmp_path / "d")
    monkeypatch.delenv("MINIBOORU_SKIP_SETUP", raising=False)
    monkeypatch.setenv("MINIBOORU_PASSWORD", "pw")
    from minibooru.web import create_app
    cl = TestClient(create_app())
    assert cl.get("/", follow_redirects=False).headers["location"].startswith("/login")
    assert "<h1>ScrapeArc</h1>" in cl.get("/login").text
    assert cl.post("/setup", data={"name": "Hack"}, follow_redirects=False).status_code == 401
    cl.post("/login", data={"password": "pw"})
    assert cl.get("/", follow_redirects=False).headers["location"].startswith("/setup")


def test_no_personal_icon_in_the_code():
    from pathlib import Path
    static = Path(core.__file__).parent / "static"
    assert sorted(p.name for p in static.iterdir() if p.is_file()) == sorted(core.ICON_FILES)
    assert not (static / "default-bg").exists()                  # no bundled background pictures either


def test_new_install_starts_plain_dark_blue(app_client):
    cl = app_client()
    cl.post("/setup", data={"name": "", "next": "/"})
    assert core.background_versions() == {}                    # no picture until you add one
    page = cl.get("/").text
    assert ':root{--bgimg:none}' in page and "--bg:#0a1224" in page   # plain dark blue page
    assert "--dim:0.5;" in page                                # a picture you add later is faded enough to read text over it


def test_existing_install_keeps_its_own_background(tmp_path):
    core.set_background("desktop", png("red"), tmp_path)
    assert core.background_versions(tmp_path)
    with core.connect(tmp_path) as c:
        assert core.brand_info(c, tmp_path)["dim"] == 0.0         # older installs keep their look


def test_example_password_is_refused(tmp_path, monkeypatch):
    monkeypatch.setattr(core, "DATA", tmp_path / "d")
    monkeypatch.setenv("MINIBOORU_PASSWORD", "change-me-to-something-long")
    from minibooru.web import create_app
    with pytest.raises(SystemExit):
        create_app()


def test_safety_headers_and_no_svg(app_client, tmp_path):
    cl = app_client()
    r = cl.get("/favicon.ico")
    assert r.headers["x-content-type-options"] == "nosniff" and r.headers["x-frame-options"] == "DENY"
    assert cl.get("/brand/icon-128.png").headers["content-security-policy"] == "sandbox"
    svg = tmp_path / "x.svg"
    svg.write_text('<svg xmlns="http://www.w3.org/2000/svg"><script>alert(1)</script></svg>')
    with core.connect() as c, pytest.raises(ValueError, match="unsupported"):
        core.ingest(c, svg, [("a", "other")])


def test_setup_picks_tag_types(app_client):
    cl = app_client()
    assert 'name="types"' in cl.get("/setup").text
    cl.post("/setup", data={"name": "", "next": "/", "types": "Person, artist,\nplace, other, person"})
    with core.connect() as c:
        assert core.type_names(c) == ["person", "artist", "place", "other"]    # cleaned, no duplicates, other stays last
        assert core.tag_rule(c) == ("other", "other", "other")                 # scraping rule is opt-in
    assert cl.post("/setup", data={"name": "", "next": "/", "types": "bad/type"}).status_code == 400


def test_clip_window_uses_this_installs_types(app_client):
    cl = app_client()
    cl.post("/setup", data={"name": "", "next": "/", "types": "person"})
    page = cl.get("/clip").text
    assert '<option value="person">' in page and '"person": "#e91e63"' in page and "idol" not in page


def test_new_install_keeps_its_colours_apart(tmp_path):
    with core.connect(tmp_path / "a") as c:
        a = core.brand_info(c, tmp_path / "a")["theme_key"]
    with core.connect(tmp_path / "b") as c:
        b = core.brand_info(c, tmp_path / "b")["theme_key"]
    assert a.startswith("mb-theme-") and b.startswith("mb-theme-") and a != b   # a fresh install starts with default colours
    import sqlite3
    raw = sqlite3.connect(tmp_path / "a" / "db.sqlite")
    raw.execute("DELETE FROM settings WHERE key='install_id'"); raw.commit(); raw.close()
    with core.connect(tmp_path / "a") as c:
        assert core.brand_info(c, tmp_path / "a")["theme_key"] == "mb-theme"      # older installs keep their saved colours

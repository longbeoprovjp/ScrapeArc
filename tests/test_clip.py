"""Saving a post from a logged-in site: the browser sends the page and the files; the server parses and stores."""
import json
from io import BytesIO

import pytest
from fastapi.testclient import TestClient

from minibooru import core
from test_copies import scene
from test_scraper import site  # noqa: F401  (local test web server fixture)

PAGE = """<html><head><meta property="og:title" content="og title"></head><body>
<article><h1>Robin and Wren</h1><time datetime="2026-10-02T08:30:00Z">Oct 2</time>
<img src="/m/a.jpg" width="800"><img src="/m/b.jpg" width="800"><img src="/m/a_small.jpg" width="200">
<div class="tags"><a href="/tag/studio_x/">Studio X</a><a href="/tag/robin/">Robin</a><a href="/tag/wren/">Wren</a><a href="/tag/misc/">misc</a></div>
<script>alert('never run')</script></article></body></html>"""


def jpg(im):
    b = BytesIO()
    im.save(b, "JPEG", quality=90)
    return b.getvalue()


@pytest.fixture
def cl(tmp_path, monkeypatch):
    monkeypatch.setattr(core, "DATA", tmp_path / "d")
    monkeypatch.delenv("MINIBOORU_PASSWORD", raising=False)
    from minibooru.web import create_app
    with core.connect() as c:
        core.set_tag_types(c, ["person", "artist"])
        core.set_tag_rule(c, "artist", "person", "other")
    return TestClient(create_app())


def parse(cl, url="https://site.example/post/7/", html=PAGE):
    return cl.post("/clip/parse", content=json.dumps({"url": url, "html": html}), headers={"Content-Type": "application/json"})


def test_parse_finds_title_date_tags_and_media(cl):
    d = parse(cl).json()
    assert d["title"] == "Robin and Wren" and d["posted"] == "2026-10-02 08:30:00" and d["already"] is None
    assert d["media"][:2] == ["https://site.example/m/a.jpg", "https://site.example/m/b.jpg"]
    assert d["tags"] == [{"name": "studio_x", "type": "artist"}, {"name": "robin", "type": "person"},
                         {"name": "wren", "type": "person"}, {"name": "misc", "type": "other"}]     # same rule as the scraper
    assert parse(cl, url="javascript:alert(1)").status_code == 400
    assert cl.post("/clip/parse", content=b"not json", headers={"Content-Type": "application/json"}).status_code == 400


def save(cl, files, meta):
    return cl.post("/clip/save", data={"meta": json.dumps(meta)}, files=[("files", f) for f in files])


def test_save_stores_post_drops_copies_and_remembers_it(cl):
    url = "https://site.example/post/7/"
    a, b = scene(71), scene(72)
    files = [("a.jpg", jpg(a), "image/jpeg"), ("b.jpg", jpg(b), "image/jpeg"),
             ("a_small.jpg", jpg(a.resize((150, 100))), "image/jpeg"),        # thumbnail copy of a
             ("notes.txt", b"hello", "text/plain")]                           # not media: ignored
    meta = {"url": url, "title": "Robin and Wren", "posted": "2026-10-02 08:30:00",
            "tags": [{"name": "Robin", "type": "person"}, {"name": "studio x", "type": "artist"}, {"name": "bad type", "type": "admin"}]}
    r = save(cl, files, meta).json()
    assert r["status"] == "new" and r["files"] == 2 and any("skipped" in n for n in r["notes"])
    with core.connect() as c:
        p = c.execute("SELECT * FROM posts WHERE id=?", (r["post_id"],)).fetchone()
        assert p["source"] == url and p["posted"] == "2026-10-02 08:30:00" and p["title"] == "Robin and Wren"
        assert {(t["name"], t["category"]) for t in core.post_tags(c, p["id"])} == {("robin", "person"), ("studio_x", "artist"), ("bad_type", "other")}
        assert c.execute("SELECT post_id FROM scraped WHERE post_url=?", (url,)).fetchone()[0] == p["id"]   # scrapes skip it later
    assert parse(cl, url=url).json()["already"] == r["post_id"]


def test_save_respects_files_you_deleted_and_rejects_empty(cl):
    url = "https://site.example/post/8/"
    a, b = jpg(scene(73)), jpg(scene(74))
    r = save(cl, [("a.jpg", a, "image/jpeg"), ("b.jpg", b, "image/jpeg")], {"url": url, "tags": []}).json()
    with core.connect() as c:
        core.remove_file(c, r["post_id"], core.post_files(c, r["post_id"])[1]["sha"])
        core.delete_post(c, r["post_id"])
    r2 = save(cl, [("a.jpg", a, "image/jpeg"), ("b.jpg", b, "image/jpeg")], {"url": url, "tags": []}).json()
    assert r2["files"] == 1                                                        # the deleted picture stays gone
    assert save(cl, [], {"url": url, "tags": []}).status_code == 400
    assert save(cl, [("a.jpg", a, "image/jpeg")], {"url": "file:///etc/passwd", "tags": []}).status_code == 400


def test_server_downloads_files_the_browser_could_not(cl, site, monkeypatch):
    from test_scraper import PAGES
    monkeypatch.setitem(PAGES, "/cdn/c.jpg", ("image/jpeg", jpg(scene(75))))
    base, hits = site
    r = save(cl, [("a.jpg", jpg(scene(76)), "image/jpeg")],
             {"url": "https://site.example/post/9/", "tags": [], "missing": [base + "/cdn/c.jpg", "javascript:x"]}).json()
    assert r["files"] == 2 and "/cdn/c.jpg" in hits


def test_clip_pages_and_cross_site_post_refused(cl):
    assert "javascript:" not in cl.get("/clip/bookmarklet").text.split("<script>")[0]   # code is built by the page's script
    page = cl.get("/clip/bookmarklet").text
    assert "const APP=location.origin" in page and "e.origin!==APP" in page and "e.source!==w" in page
    assert "e.source!==opener" in cl.get("/clip").text
    r = cl.post("/clip/save", data={"meta": "{}"}, headers={"Origin": "https://evil.example"})
    assert r.status_code == 403
    assert "/clip/bookmarklet" in cl.get("/scrape").text


def test_popup_closes_after_save_and_page_shows_a_bubble(cl):
    popup = cl.get("/clip").text
    assert "tell('saving','Saving…')" in popup and "tell('saved'" in popup and "window.close()" in popup
    assert "tell('error'" in popup                                    # on failure the window stays open
    bml = cl.get("/clip/bookmarklet").text
    assert "m.type==='status'" in bml and "textContent=t" in bml      # bubble text is set as text, never as HTML
    code = bml[bml.index("function mbClipRun(APP)"):bml.index("const APP=location.origin")]   # shared with the toolbar button
    assert "innerHTML" not in code and "insertAdjacentHTML" not in code     # nothing from the popup is turned into page HTML


def test_history_heading_shows_even_when_empty(cl):
    page = cl.get("/scrape").text
    assert "<h3>History</h3>" in page and "No scrape runs yet" in page


def test_bookmarked_filter_shows_only_bookmark_saves(cl, tmp_path):
    from PIL import Image
    r = save(cl, [("a.jpg", jpg(scene(81)), "image/jpeg")], {"url": "https://site.example/post/9/", "title": "Saved", "tags": []}).json()
    Image.new("RGB", (40, 30), "navy").save(tmp_path / "up.png")
    with core.connect() as c:
        other, _ = core.ingest(c, tmp_path / "up.png", ["x"], title="Not bookmarked")
        rows, total = core.search(c, "", view="bookmarked")
        assert [p["id"] for p in rows] == [r["post_id"]] and total == 1
    page = cl.get("/?view=bookmarked").text
    assert 'class="fchip on" href="/?view=bookmarked"' in page and f'/post/{r["post_id"]}' in page and f'/post/{other}"' not in page


# ---------- iPhone: home-screen app ----------
def test_home_screen_app_manifest_and_tags(cl):
    m = cl.get("/manifest.webmanifest")
    assert m.status_code == 200 and m.headers["content-type"].startswith("application/manifest+json")
    j = m.json()
    assert j["display"] == "standalone" and j["start_url"] == "/" and j["icons"][0]["src"].startswith("/brand/icon-192.png")
    page = cl.get("/").text
    for bit in ('rel="manifest"', 'apple-mobile-web-app-capable', 'viewport-fit=cover', 'class="appnav"', "safe-area-inset-top"):
        assert bit in page
    assert 'rel="manifest"' in cl.get("/login").text
    assert 'id="a2hs"' in page and "Add to Home Screen" in page and 'id="iphone"' not in cl.get("/clip/bookmarklet").text


def test_with_a_password_only_the_manifest_skips_login(tmp_path, monkeypatch):
    monkeypatch.setattr(core, "DATA", tmp_path / "d")
    monkeypatch.setenv("MINIBOORU_PASSWORD", "a-long-test-password")
    from minibooru.web import create_app
    cl = TestClient(create_app())
    assert cl.get("/manifest.webmanifest").status_code == 200                      # phones read it before you log in
    assert cl.get("/clip/bookmarklet", follow_redirects=False).status_code == 303
    assert cl.post("/clip/api/save", content=b"{}").status_code == 401             # the Shortcut endpoint is gone (login required)


def test_toolbar_button_download_has_icon_and_same_code_as_bookmark(cl):
    import io, json, zipfile
    r = cl.get("/clip/extension.zip", params={"app": "http://box.lan:8090"})
    assert r.status_code == 200 and r.headers["content-type"] == "application/zip"
    z = zipfile.ZipFile(io.BytesIO(r.content))
    names = set(z.namelist())
    assert {f"save-to-app/icon-{n}.png" for n in (16, 32, 48, 128)} <= names
    man = json.loads(z.read("save-to-app/manifest.json"))
    assert man["manifest_version"] == 3 and man["permissions"] == ["activeTab", "scripting"] and "host_permissions" not in man
    bg = z.read("save-to-app/background.js").decode()
    assert 'const APP = "http://box.lan:8090";' in bg and "function mbClipRun(APP)" in bg and "world: 'MAIN'" in bg
    assert cl.get("/clip/extension.zip", params={"app": "javascript:alert(1)"}).status_code == 400
    page = cl.get("/clip/bookmarklet").text
    assert "function mbClipRun(APP)" in page and "mbClipRun.toString()" in page and 'id="extdl"' in page

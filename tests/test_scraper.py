import httpx
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from io import BytesIO

import httpx
import pytest
from PIL import Image

from minibooru import core, scraper


def jpg(color):
    b = BytesIO()
    Image.new("RGB", (60, 40), color).save(b, "JPEG")
    return b.getvalue()


PAGES = {
    "/robots.txt": ("text/plain", b"User-agent: *\nDisallow: /private/\n"),
    "/list/": ("text/html", """<html><body>
      <article class='post'><a href='/post/1/'><img src='/t1.jpg' alt='Robin on a branch'></a>
        <a href='/species/robin/'>Robin</a><a href='/place/garden/'>Garden</a></article>
      <article class='post'><a href='/post/2/'><img src='/t2.jpg' alt='Sparrow at dusk'></a><a href='/species/sparrow/'>Sparrow</a></article>
      <article class='post'><a href='/post/3/'><img src='/t3.jpg' alt='오늘의 직캠 Robin'></a><a href='/species/robin/'>Robin</a></article>
      <a rel='next' href='/list/2/'>Next</a></body></html>""".encode()),
    "/list/2/": ("text/html", b"<article><a href='/post/4/'><img src='/t4.jpg' alt='Another ROBIN'></a></article>"),
    "/post/1/": ("text/html", b"<main><img src='/logo.png'><img src='/big1.jpg' width='800'>"
                              b"<a href='/species/robin/'>Robin</a><a href='/species/redbreast/'>Redbreast</a></main>"),
    "/post/3/": ("text/html", b"<video src='/clip3.jpg'></video>"),
    "/post/4/": ("text/html", b"<meta property='og:image' content='/big4.jpg'>"),
    "/big1.jpg": ("image/jpeg", jpg("red")), "/clip3.jpg": ("image/jpeg", jpg("blue")),
    "/big4.jpg": ("image/jpeg", jpg("green")),
}


@pytest.fixture
def site(monkeypatch):
    hits = []

    class H(BaseHTTPRequestHandler):
        def do_GET(self):
            hits.append(self.path)
            ctype, body = PAGES.get(self.path, (None, None))
            self.send_response(200 if body else 404)
            if body:
                self.send_header("Content-Type", ctype)
            self.end_headers()
            self.wfile.write(body or b"")

        def log_message(self, *a):
            pass

    srv = HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    monkeypatch.setenv("MINIBOORU_ALLOW_PRIVATE", "1")
    yield f"http://127.0.0.1:{srv.server_port}", hits
    srv.shutdown()


@pytest.fixture
def db(tmp_path, monkeypatch):
    monkeypatch.setenv("MINIBOORU_SLEEP", "0")
    monkeypatch.setattr(core, "DATA", tmp_path / "data")
    with core.connect() as c:
        core.set_tag_types(c, ["person", "artist"])
        core.set_tag_rule(c, "artist", "person", "other")    # e.g. a site that lists the author first
        yield c


def opts(**kw):
    return scraper.Options(keywords=kw.pop("keywords", ["robin"]), sleep=0, **kw)


def test_title_filter_tags_and_media(site, db):
    base, hits = site
    st = scraper.scrape(db, base + "/list/", opts(pages=1), log=lambda m: None)
    assert (st["seen"], st["matched"], st["new"], st["filtered"]) == (3, 2, 2, 1)
    titles = {r["title"] for r in db.execute("SELECT title FROM posts")}
    assert titles == {"Robin on a branch", "오늘의 직캠 Robin"}
    tags = {r[0]: r[1] for r in db.execute("SELECT t.name, t.category FROM post_tags pt JOIN tags t ON t.id=pt.tag_id")}
    # typed by position: post-page tags first (Robin, Redbreast), then card-only ones (Garden)
    assert (tags["robin"], tags["redbreast"], tags["garden"]) == ("artist", "person", "other")
    assert "/post/2/" not in hits            # non-matching post page never fetched
    assert "/logo.png" not in hits           # logo skipped in favour of the real image


def test_rerun_skips_and_pagination(site, db):
    base, _ = site
    scraper.scrape(db, base + "/list/", opts(pages=1), log=lambda m: None)
    st = scraper.scrape(db, base + "/list/", opts(pages=2), log=lambda m: None)
    assert st["already"] == 2 and st["new"] == 1       # page 2's post is new


def test_korean_keyword_and_limit(site, db):
    base, _ = site
    st = scraper.scrape(db, base + "/list/", opts(keywords=["직캠"], pages=1), log=lambda m: None)
    assert st["new"] == 1
    st = scraper.scrape(db, base + "/list/", opts(pages=2, limit=1), log=lambda m: None)
    assert st["seen"] == 1


def test_robots_and_private_guard(site, db, monkeypatch):
    base, _ = site
    msgs = []
    st = scraper.scrape(db, base + "/private/x", opts(), log=msgs.append)
    assert st["error"] == 1 and any("robots" in m for m in msgs)
    monkeypatch.delenv("MINIBOORU_ALLOW_PRIVATE")
    st = scraper.scrape(db, base + "/list/", opts(), log=msgs.append)
    assert any("private" in m for m in msgs)


def test_keywords_required(db):
    with pytest.raises(scraper.ScrapeError):
        scraper.scrape(db, "http://x/", scraper.Options(keywords=[" "]))


def test_video_post(site, db, tmp_path):
    import shutil
    import subprocess
    if not shutil.which("ffmpeg"):
        pytest.skip("ffmpeg missing")
    mp4 = tmp_path / "c.mp4"
    subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "testsrc=size=160x120:rate=10:duration=1",
                    "-pix_fmt", "yuv420p", str(mp4)], check=True)
    PAGES["/vlist/"] = ("text/html", b"<article><a href='/v/1/'><img src='/t.jpg' alt='Robin video clip'></a>"
                                     b"<a href='/species/robin/'>Robin</a></article>")
    PAGES["/v/1/"] = ("text/html", b"<video controls><source src='/clip.mp4' type='video/mp4'></video>")
    PAGES["/clip.mp4"] = ("video/mp4", mp4.read_bytes())
    st = scraper.scrape(db, site[0] + "/vlist/", opts(), log=lambda m: None)
    assert st["new"] == 1
    row = db.execute("SELECT * FROM posts").fetchone()
    assert row["mime"] == "video/mp4" and (row["width"], row["height"]) == (160, 120)
    assert (core.DATA / "thumbs" / f"{row['sha256']}.jpg").exists()


def _tags_of(db):
    return {r[0] for r in db.execute("SELECT t.name FROM post_tags pt JOIN tags t ON t.id=pt.tag_id")}


def test_tags_only_on_post_page_with_tag_class(site, db):
    PAGES["/l3/"] = ("text/html", b"<article><a href='/p/9/'><img src='/t.jpg' alt='Robin only'></a></article>")
    PAGES["/p/9/"] = ("text/html", b"<main><img src='/big1.jpg' width='500'><nav><a href='/about'>About</a></nav>"
                                   b"<div class='tags'><a href='/robin'>Robin</a><a href='/red-breast'>Red Breast</a></div></main>")
    scraper.scrape(db, site[0] + "/l3/", opts(), log=lambda m: None)
    assert _tags_of(db) == {"robin", "red_breast"}          # nav link 'About' excluded


def test_tags_unmarked_links_card_empty(site, db):
    PAGES["/l4/"] = ("text/html", b"<article><a href='/p/8/'><img src='/t.jpg' alt='Robin plain'></a></article>")
    PAGES["/p/8/"] = ("text/html", b"<main><img src='/big4.jpg' width='500'><a href='/species/robin/'>Robin</a>"
                                   b"<a href='/species/wren/'>Wren</a><a href='/contact/'>Contact</a></main>")
    scraper.scrape(db, site[0] + "/l4/", opts(), log=lambda m: None)
    assert _tags_of(db) == {"robin", "wren"}


def test_watch_runs_when_due_and_reschedules(site, db):
    import time
    from minibooru import watcher
    db.execute("INSERT INTO watches(url, keywords, interval_min, next_run) VALUES (?,?,60,0)", (site[0] + "/list/", "robin"))
    db.commit()
    assert watcher.run_due() == 1
    w = db.execute("SELECT * FROM watches").fetchone()
    assert "new" in w["last_result"] and w["next_run"] > time.time() + 3000
    assert watcher.run_due() == 0                                   # not due again yet
    db.execute("UPDATE watches SET next_run=0"); db.commit()
    watcher.run_due()
    assert "0 new" in db.execute("SELECT last_result FROM watches").fetchone()[0]   # reruns skip imported
    assert db.execute("SELECT COUNT(*) FROM posts").fetchone()[0] == 3     # follows page 2 automatically


def test_watch_offline_is_quiet(db):
    from minibooru import watcher
    db.execute("INSERT INTO watches(url, keywords, next_run) VALUES ('http://localhost:1/', 'x', 0)"); db.commit()
    import os; os.environ["MINIBOORU_ALLOW_PRIVATE"] = "1"
    try:
        watcher.run_due()
    finally:
        del os.environ["MINIBOORU_ALLOW_PRIVATE"]
    assert "unreachable" in db.execute("SELECT last_result FROM watches").fetchone()[0]
    assert db.execute("SELECT COUNT(*) FROM scrape_runs").fetchone()[0] == 1          # and the attempt is in History


def test_deleted_post_can_be_scraped_again(site, db):
    scraper.scrape(db, site[0] + "/list/", opts(pages=1), log=lambda m: None)
    db.execute("DELETE FROM posts")            # simulates a delete that left the history row behind
    db.commit()
    st = scraper.scrape(db, site[0] + "/list/", opts(pages=1), log=lambda m: None)
    assert st["new"] == 2 and st["already"] == 0


def test_guess_next_patterns():
    g = scraper.guess_next
    assert g("http://s/list/") == "http://s/list/page/2/"
    assert g("http://s/list") == "http://s/list/page/2/"
    assert g("http://s/list/page/2/") == "http://s/list/page/3/"
    assert g("http://s/list/page/9") == "http://s/list/page/10"
    assert g("http://s/l?x=1&page=3") == "http://s/l?x=1&page=4"


def test_scan_limit_spans_pages_without_next_links(site, db):
    # 15 posts per page, no <a rel=next>: pages are only reachable via /page/N/
    def listing(start):
        return "".join(f"<article><a href='/q/{i}/'><img src='/t.jpg' alt='{'Robin' if i % 7 == 0 else 'Other'} {i}'></a></article>"
                       for i in range(start, start + 15)).encode()
    PAGES["/many/"] = ("text/html", listing(1))
    PAGES["/many/page/2/"] = ("text/html", listing(16))
    PAGES["/many/page/3/"] = ("text/html", listing(31))
    for i in range(1, 46):
        if i % 7 == 0:
            PAGES[f"/q/{i}/"] = ("text/html", b"<video src='/clip3.jpg'></video>")
    # limit = posts IMPORTED; it scans as far as needed (matches are 7,14,21,28,35,42)
    st = scraper.scrape(db, site[0] + "/many/", opts(limit=2), log=lambda m: None)
    assert st["new"] + st["duplicate"] == 2 and st["seen"] == 14
    st = scraper.scrape(db, site[0] + "/many/", opts(limit=2), log=lambda m: None)
    assert st["already"] == 2 and st["new"] + st["duplicate"] == 2 and st["seen"] == 28    # crossed onto page 2; already-imported don't count
    st = scraper.scrape(db, site[0] + "/many/", opts(limit=500), log=lambda m: None)
    assert st["new"] + st["duplicate"] == 2 and st["seen"] == 45                           # ran out of pages (page 4 is a 404): clean stop
    assert st["error"] == 0


def test_multiple_keywords_any_match(site, db):
    st = scraper.scrape(db, site[0] + "/list/", opts(keywords=["sparrow", "직캠"], pages=1), log=lambda m: None)
    assert st["matched"] == 2 and st["new"] == 1        # either keyword matches; the sparrow post has no media page


def test_tags_typed_by_position(site, db):
    PAGES["/r/"] = ("text/html", b"<article><a href='/p/r1/'><img src='/t.jpg' alt='Robin shot'></a>"
                                 b"<div class='tags'><a href='/a'>Jane Doe</a><a href='/b'>Blue Jay</a><a href='/c'>Robin Red</a><a href='/d'>Fancam</a></div></article>")
    PAGES["/p/r1/"] = ("text/html", b"<video src='/clip3.jpg'></video>")
    msgs = []
    scraper.scrape(db, site[0] + "/r/", opts(), log=msgs.append)
    cats = {r[0]: r[1] for r in db.execute("SELECT name, category FROM tags")}
    assert cats == {"jane_doe": "artist", "blue_jay": "person", "robin_red": "person", "fancam": "other"}
    assert any("artist: jane_doe · person: blue_jay, robin_red · other: fancam" in m for m in msgs)   # shown in the progress log


@pytest.mark.parametrize("labels,want", [
    (["A"], ["artist"]), (["A", "B"], ["artist", "other"]), (["A", "B", "C"], ["artist", "person", "other"]),
    (["A", "B", "C", "D", "E"], ["artist", "person", "person", "person", "other"]),
    (["A", "B", "a", "C"], ["artist", "person", "other"])])           # duplicate names keep their first position
def test_type_by_position(labels, want):
    assert [k for _, k in scraper.type_by_position(labels, ("artist", "person", "other"))] == want
    assert {k for _, k in scraper.type_by_position(labels)} == {"other"}       # no rule set: everything is other


def test_stop_finishes_the_post_in_progress(site, db):
    import threading
    ev = threading.Event()
    msgs = []

    def log(m):
        msgs.append(m)
        if m.startswith("+ "):          # press Stop while the first post is being imported
            ev.set()
    st = scraper.scrape(db, site[0] + "/list/", opts(cancel=ev), log=log)
    assert st["cancelled"] == 1 and st["new"] == 1                    # that post was finished with all its tags
    pid = db.execute("SELECT id FROM posts").fetchone()[0]
    assert {r[0] for r in core.post_tags(db, pid)} == {"robin", "redbreast", "garden"}


def test_delete_and_block_tag(db, tmp_path):
    from PIL import Image
    Image.new("RGB", (20, 20), "red").save(tmp_path / "a.png")
    pid, _ = core.ingest(db, tmp_path / "a.png", ["robin", "badtag"], title="t")
    assert core.delete_tag(db, "badtag", block=True)
    assert {r[0] for r in core.post_tags(db, pid)} == {"robin"}
    core.add_tags(db, pid, ["badtag", "wren"])                       # blocked tag is not re-added
    assert {r[0] for r in core.post_tags(db, pid)} == {"robin", "wren"}
    assert not core.delete_tag(db, "nonexistent")


def test_tag_page_and_delete_route(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from PIL import Image
    from minibooru.web import create_app
    monkeypatch.setattr(core, "DATA", tmp_path / "d")
    Image.new("RGB", (20, 20), "red").save(tmp_path / "a.png")
    Image.new("RGB", (20, 20), "blue").save(tmp_path / "b.png")
    with core.connect() as c:
        core.ingest(c, tmp_path / "a.png", ["robin", "bird"], title="First")
        core.ingest(c, tmp_path / "b.png", ["wren"], title="Second")
    cl = TestClient(create_app())
    r = cl.get("/tag/robin")
    assert r.status_code == 200 and "First" in r.text and "Second" not in r.text
    assert cl.get("/tag/nope").status_code == 404
    assert cl.post("/tags/category", data={"name": "robin", "category": "species"}).status_code == 400   # no such type
    cl.post("/tags/types/add", data={"name": "species"})
    cl.post("/tags/category", data={"name": "robin", "category": "species"})
    with core.connect() as c:
        assert c.execute("SELECT category FROM tags WHERE name='robin'").fetchone()[0] == "species"
    cl.post("/tag/robin/delete", data={"block": "1"})
    assert cl.get("/tag/robin").status_code == 404 and "robin" in cl.get("/tags").text   # listed under Blocked


def test_bulk_delete_route(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from PIL import Image
    from minibooru.web import create_app
    monkeypatch.setattr(core, "DATA", tmp_path / "d")
    Image.new("RGB", (20, 20), "red").save(tmp_path / "a.png")
    with core.connect() as c:
        core.ingest(c, tmp_path / "a.png", ["robin", "wren", "keep"], title="x")
    cl = TestClient(create_app())
    cl.post("/tags/bulk-delete", data={"names": ["robin", "wren"], "block": "1"})
    with core.connect() as c:
        assert [r[0] for r in c.execute("SELECT name FROM tags")] == ["keep"]
        assert {r[0] for r in c.execute("SELECT name FROM blocked_tags")} == {"robin", "wren"}


# ---------- upload date, sorting, bulk delete, run delete ----------
@pytest.mark.parametrize("raw,want", [
    ("2024-05-01", "2024-05-01 00:00:00"), ("2024-05-01T10:30:00+09:00", "2024-05-01 01:30:00"),
    ("2024-05-01T10:30:00Z", "2024-05-01 10:30:00"), ("Posted on May 3, 2024", "2024-05-03 00:00:00"),
    ("1714521600", "2024-05-01 00:00:00"), ("yesterday", None), ("", None), ("0001-01-01", None)])
def test_parse_date(raw, want):
    assert scraper.parse_date(raw) == want


def test_find_posted_sources():
    from bs4 import BeautifulSoup
    f = lambda html, **kw: scraper.find_posted(BeautifulSoup(html, "html.parser"), BeautifulSoup(html, "html.parser"), **kw)
    assert f("<article><time datetime='2024-02-03T04:05:06Z'>x</time></article>") == "2024-02-03 04:05:06"
    assert f("<article data-date-short='2023-12-25'></article>") == "2023-12-25 00:00:00"
    assert f("<meta property='article:published_time' content='2022-01-02'>") == "2022-01-02 00:00:00"
    assert f("<script type='application/ld+json'>{\"datePublished\":\"2021-07-08\"}</script>") == "2021-07-08 00:00:00"
    assert f("<span class='when'>2020-09-10</span>", selector=".when") == "2020-09-10 00:00:00"
    assert f("<p>no date here</p>") is None


def test_scraped_posts_sort_by_site_upload_date(site, db, tmp_path):
    from PIL import Image
    # imported in this order, but uploaded on the site in the opposite order
    for name, color, posted in (("a", "red", "2024-01-01 00:00:00"), ("b", "blue", "2024-06-01 00:00:00"),
                                ("c", "green", None)):
        Image.new("RGB", (20, 20), color).save(tmp_path / f"{name}.png")
        core.ingest(db, tmp_path / f"{name}.png", [], title=name, posted=posted)
    # c has no site date -> falls back to import time (now), so it is the newest
    assert [p["title"] for p in core.search(db, "")[0]] == ["c", "b", "a"]
    assert [p["title"] for p in core.search(db, "", sort="old")[0]] == ["a", "b", "c"]
    db.execute("UPDATE posts SET posted='2025-01-01 00:00:00' WHERE title='a'")
    assert [p["title"] for p in core.search(db, "")[0]][:2] == ["c", "a"]


def test_scrape_stores_posted_date(site, db):
    PAGES["/post/1/"] = ("text/html", b"<main><time datetime='2024-03-04T05:06:07Z'></time>"
                                      b"<img src='/big1.jpg' width='800'></main>")
    try:
        scraper.scrape(db, site[0] + "/list/", opts(limit=1), log=lambda m: None)
    finally:
        PAGES["/post/1/"] = ("text/html", b"<main><img src='/logo.png'><img src='/big1.jpg' width='800'>"
                                          b"<a href='/species/robin/'>Robin</a><a href='/species/redbreast/'>Redbreast</a></main>")
    assert db.execute("SELECT posted FROM posts").fetchone()[0] == "2024-03-04 05:06:07"


def test_ui_routes_sort_bulk_delete_runs(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from PIL import Image
    from minibooru.web import create_app
    monkeypatch.setattr(core, "DATA", tmp_path / "d")
    ids = []
    with core.connect() as c:
        for name, color, posted in (("old_one", "red", "2020-01-01"), ("new_one", "blue", "2024-01-01"),
                                    ("keep_me", "green", "2022-01-01")):
            Image.new("RGB", (20, 20), color).save(tmp_path / f"{name}.png")
            ids.append(core.ingest(c, tmp_path / f"{name}.png", ["x"], title=name, posted=posted)[0])
        c.execute("INSERT INTO scrape_runs(url, keywords) VALUES ('u1','k'), ('u2','k'), ('u3','k')")
    cl = TestClient(create_app())
    t = cl.get("/").text
    assert t.index("new_one") < t.index("keep_me") < t.index("old_one")            # newest first by default
    t = cl.get("/?sort=old").text
    assert t.index("old_one") < t.index("keep_me") < t.index("new_one")
    assert "sort=old" in cl.get("/tag/x?sort=old").text or "Oldest first" in cl.get("/tag/x?sort=old").text
    r = cl.post("/posts/bulk-delete", data={"ids": [ids[0], ids[1]], "next": "/?sort=old"}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/?sort=old"
    r = cl.post("/posts/bulk-delete", data={"ids": [ids[2]], "next": "//evil.example"}, follow_redirects=False)
    assert r.headers["location"] == "/"                                               # no open redirect
    with core.connect() as c:
        assert c.execute("SELECT COUNT(*) FROM posts").fetchone()[0] == 0
    assert not list((tmp_path / "d" / "files").rglob("*.png")) and not list((tmp_path / "d" / "thumbs").glob("*.jpg"))
    with core.connect() as c:
        rid = [r[0] for r in c.execute("SELECT id FROM scrape_runs ORDER BY id")]
    cl.post(f"/scrape-runs/{rid[0]}/delete")
    cl.post("/scrape-runs/bulk-delete", data={"ids": rid[1:]})
    with core.connect() as c:
        assert c.execute("SELECT COUNT(*) FROM scrape_runs").fetchone()[0] == 0
    assert cl.get("/scrape").status_code == 200


def test_grid_shows_tag_chips(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from PIL import Image
    from minibooru.web import create_app
    monkeypatch.setattr(core, "DATA", tmp_path / "d")
    Image.new("RGB", (20, 20), "red").save(tmp_path / "a.png")
    with core.connect() as c:
        core.ingest(c, tmp_path / "a.png", ["robin", "garden", "t1", "t2", "t3", "t4", "t5", "t6", "t7"], title="x")
    html = TestClient(create_app()).get("/").text
    assert html.count('class="chip"') == 8 and 'href="/tag/robin"' in html and "+1" in html


def test_find_all_media_gallery_only():
    from bs4 import BeautifulSoup
    from minibooru import scraper
    html = """<html><body><header><img src="/logo.png"></header>
    <article><h1>Post</h1>
      <img src="/a.jpg"><a href="/full/b.jpg"><img src="/thumbs/b_small.jpg"></a><img src="/a.jpg">
      <img src="/tiny.jpg" width="40" height="40">
      <div class="related-posts"><img src="/rel.jpg"></div>
      <a href="/other-post"><img src="/other.jpg"></a>
    </article><aside><img src="/side.jpg"></aside></body></html>"""
    got = scraper.find_all_media(BeautifulSoup(html, "html.parser"), "https://x.test/post/1", scraper.Options(keywords=["k"]), "https://x.test/post/1")
    assert got == ["https://x.test/a.jpg", "https://x.test/full/b.jpg"]


def test_rescan_stops_at_first_known_post(site, db):
    quiet = lambda m: None
    scraper.scrape(db, site[0] + "/list/", opts(limit=1), log=quiet)                    # saves post 1 only
    st = scraper.scrape(db, site[0] + "/list/", opts(limit=10, stop_on_known=True), log=quiet)
    assert st["stopped"] == 1 and st["new"] == 0                                         # hit post 1, stopped for this run
    assert db.execute("SELECT COUNT(*) FROM posts").fetchone()[0] == 1
    st = scraper.scrape(db, site[0] + "/list/", opts(limit=10), log=quiet)              # a normal scrape keeps going
    assert st["new"] == 2 and st["stopped"] == 0


def test_watch_result_mentions_stop(site, db):
    from minibooru import watcher
    scraper.scrape(db, site[0] + "/list/", opts(limit=1), log=lambda m: None)
    db.execute("INSERT INTO watches(url, keywords, interval_min, next_run) VALUES (?,?,60,0)", (site[0] + "/list/", "robin"))
    db.commit()
    watcher.run_due()
    assert "stopped at a post already saved" in db.execute("SELECT last_result FROM watches").fetchone()[0]


def dated_site(monkeypatch):
    items = [("m1", "red", "2024-05-10"), ("m2", "green", "2024-05-01"), ("m3", "blue", "2024-04-20"),
             ("m4", "yellow", "2024-04-10"), ("m5", "black", "2024-03-01")]
    html = "".join(f"<article><a href='/{n}.jpg'><img alt='Robin {n}'></a><time datetime='{d}'>x</time></article>" for n, _, d in items)
    monkeypatch.setitem(PAGES, "/dl/", ("text/html", html.encode()))
    for n, color, _ in items:
        monkeypatch.setitem(PAGES, f"/{n}.jpg", ("image/jpeg", jpg(color)))


def test_scrape_stops_at_date(site, db, monkeypatch):
    dated_site(monkeypatch)
    msgs = []
    st = scraper.scrape(db, site[0] + "/dl/", opts(limit=5000, until="2024-04-25"), log=msgs.append)
    assert st["new"] == 2 and st["stopped"] == 2                        # m1, m2 imported; m3 and m4 are older: stop
    assert db.execute("SELECT COUNT(*) FROM posts").fetchone()[0] == 2
    assert "/m5.jpg" not in site[1]                                      # never reached the oldest one
    assert any("dated 2024-05-10" in m for m in msgs) and any("posted 2024-05-10" in m for m in msgs)
    assert db.execute("SELECT kind FROM scrape_runs").fetchone()[0] == "manual"


def test_scrape_cancel(site, db):
    import threading
    ev = threading.Event(); ev.set()
    st = scraper.scrape(db, site[0] + "/list/", opts(cancel=ev), log=lambda m: None)
    assert st["cancelled"] == 1 and st["new"] == 0


def test_watch_logs_kind_and_date_mode(site, db, monkeypatch):
    from minibooru import watcher
    dated_site(monkeypatch)
    db.execute("INSERT INTO watches(url, keywords, interval_min, until, next_run) VALUES (?,?,60,'2024-04-25',0)", (site[0] + "/dl/", "robin"))
    db.commit()
    assert watcher.run_due() == 1
    wid = db.execute("SELECT id FROM watches").fetchone()[0]
    assert any("posted 2024-05-10" in line for line in watcher.logs[wid])      # live log kept per watch
    assert "reached the date limit" in db.execute("SELECT last_result FROM watches").fetchone()[0]
    assert db.execute("SELECT kind FROM scrape_runs").fetchone()[0] == "auto"
    assert watcher.current is None and watcher.running_wid is None             # lock state cleaned up


def test_run_logs_why_it_stopped(site, db):
    base, _ = site
    logs = []
    scraper.scrape(db, base + "/list/", opts(pages=1), log=logs.append)
    assert any("safety cap of 1 listing pages" in m for m in logs)       # never stops silently on the page cap
    logs.clear()
    scraper.scrape(db, base + "/list/", opts(limit=1), log=logs.append)
    assert any("limit of 1 imported posts" in m for m in logs)


def test_date_mode_caps_are_not_the_real_stop():
    from minibooru import watcher
    assert scraper.Options(keywords=["x"]).pages >= 10000                 # old cap (500 pages) ended long date runs early
    assert watcher.SAFETY_LIMIT >= 100000


def test_timeouts_are_retried_then_give_up(monkeypatch):
    import httpx
    monkeypatch.setenv("MINIBOORU_ALLOW_PRIVATE", "1")
    calls, logs = [], []

    def handler(request):
        calls.append(1)
        if len(calls) == 1:
            raise httpx.ReadTimeout("The read operation timed out", request=request)
        return httpx.Response(200, text="ok")

    f = scraper.Fetcher(opts(), logs.append)
    f.client = httpx.Client(transport=httpx.MockTransport(handler))
    monkeypatch.setattr(f, "_sleep", lambda s: None)
    monkeypatch.setattr(f, "_guard", lambda u: None)
    assert f._request("http://example.test/").text == "ok"          # one timeout -> retried, run goes on
    assert len(calls) == 2 and any("timed out" in m for m in logs) and f.errors == 0

    f.client = httpx.Client(transport=httpx.MockTransport(lambda r: (_ for _ in ()).throw(httpx.ReadTimeout("slow", request=r))))
    with pytest.raises(scraper.ScrapeError, match="giving up"):     # one dead request: only that post is skipped
        f._request("http://example.test/")
    for _ in range(scraper.GIVE_UP_IN_A_ROW - 2):
        with pytest.raises(scraper.ScrapeError, match="giving up"):
            f._request("http://example.test/")
    with pytest.raises(scraper.ScrapeError, match="too many"):     # the site stays dead: the run stops
        f._request("http://example.test/")


def test_one_dead_request_does_not_stop_the_run(monkeypatch):
    """Regression: a post whose request timed out on every try used up the in-a-row budget,
    so the very next timeout (even one that would have recovered) ended the whole run."""
    import httpx
    monkeypatch.setenv("MINIBOORU_ALLOW_PRIVATE", "1")
    calls = []

    def handler(request):
        calls.append(1)
        if len(calls) <= scraper.ATTEMPTS + 1:     # first request: every try times out; second: one timeout
            raise httpx.ReadTimeout("The read operation timed out", request=request)
        return httpx.Response(200, text="ok")

    f = scraper.Fetcher(opts(), lambda m: None)
    f.client = httpx.Client(transport=httpx.MockTransport(handler))
    monkeypatch.setattr(f, "_sleep", lambda s: None)
    monkeypatch.setattr(f, "_guard", lambda u: None)
    with pytest.raises(scraper.ScrapeError, match="giving up"):
        f._request("http://example.test/a")
    assert f._request("http://example.test/b").text == "ok"
    assert f.errors == 0


def test_run_log_keeps_only_the_last_lines():
    log = scraper.RunLog(cap=3)
    for i in range(10):
        log.append(f"line {i}")
    assert list(log) == ["line 7", "line 8", "line 9"] and log.dropped == 7
    assert "\n".join(log).endswith("line 9")             # still renders like a plain list


@pytest.mark.parametrize("secs, want", [(None, "—"), (0, "0s"), (45, "45s"), (725, "12m 05s"),
                                        (11220, "3h 07m"), (187800, "2d 04h 10m")])
def test_fmt_duration(secs, want):
    assert scraper.fmt_duration(secs) == want


def test_run_history_records_start_finish_and_duration(site, db):
    base, _ = site
    logs = []
    scraper.scrape(db, base + "/list/", opts(limit=1), log=logs.append)
    r = db.execute("SELECT started, finished, duration_s FROM scrape_runs ORDER BY id DESC").fetchone()
    assert r["started"] <= r["finished"] and r["duration_s"] is not None and r["duration_s"] >= 0
    assert "took" in logs[-1]


def test_old_run_history_is_migrated(tmp_path):
    import sqlite3
    old = sqlite3.connect(tmp_path / "db.sqlite")
    old.execute("CREATE TABLE scrape_runs (id INTEGER PRIMARY KEY, url TEXT NOT NULL, keywords TEXT NOT NULL,"
                " started TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, seen INTEGER, matched INTEGER, new INTEGER,"
                " duplicate INTEGER, already INTEGER, errors INTEGER)")
    old.execute("INSERT INTO scrape_runs(url, keywords, started) VALUES ('u', 'k', '2026-11-14 15:47:49')")
    old.commit()
    old.close()
    with core.connect(tmp_path) as c:
        r = c.execute("SELECT started, finished, duration_s FROM scrape_runs").fetchone()
    assert r["finished"] == "2026-11-14 15:47:49" and r["duration_s"] is None   # old 'started' was the finish time


def test_pages_show_run_time(site, tmp_path, monkeypatch):
    import time as _t
    from fastapi.testclient import TestClient
    from minibooru import watcher
    from minibooru.web import create_app
    monkeypatch.setattr(core, "DATA", tmp_path / "d")
    with core.connect() as c:
        c.execute("INSERT INTO scrape_runs(url, keywords, finished, duration_s) VALUES ('u', 'k', '2026-11-14 15:00:00', 3725)")
    cl = TestClient(create_app())
    base, _ = site
    r = cl.post("/scrape", data={"url": base + "/list/", "keywords": "robin", "limit": 1}, follow_redirects=False)
    jid = r.headers["location"].rsplit("/", 1)[-1]
    page = cl.get(f"/scrape/{jid}").text
    assert "Running for <b>" in page                                          # live run page
    for _ in range(300):
        if "Scrape finished" in page:
            break
        _t.sleep(0.1)
        page = cl.get(f"/scrape/{jid}").text
    assert "Took <b>" in page                                                 # finished run page
    t = cl.get("/scrape").text
    assert "<th>Took</th>" in t and "1h 02m" in t                             # history column

    monkeypatch.setattr(watcher, "current", "manual scrape of x")             # a run in progress
    monkeypatch.setattr(watcher, "started_at", _t.time() - 125)
    assert "running for 2m 05s" in cl.get("/scrape/watches").text


def test_post_with_full_size_and_thumbnail_keeps_one_copy(site, db, monkeypatch):
    """The site shows each picture twice (full size + a small strip copy): only the full-size one is saved."""
    from test_copies import scene
    def enc(im, **kw):
        b = BytesIO()
        im.save(b, "JPEG", **kw)
        return b.getvalue()
    pages = {
        "/g/": ("text/html", b"<article><a href='/gpost/'><img src='/s.jpg' alt='Robin gallery'></a></article>"),
        "/gpost/": ("text/html", b"<main><img src='/full_a.jpg'><img src='/full_b.jpg'>"
                                 b"<div><img src='/small_a.jpg'><img src='/small_b.jpg'></div></main>"),
        "/full_a.jpg": ("image/jpeg", enc(scene(11), quality=92)), "/full_b.jpg": ("image/jpeg", enc(scene(12), quality=92)),
        "/small_a.jpg": ("image/jpeg", enc(scene(11).resize((150, 100)))),
        "/small_b.jpg": ("image/jpeg", enc(scene(12).resize((150, 100)))),
    }
    monkeypatch.setitem(PAGES, "/g/", pages["/g/"])
    for k, v in pages.items():
        monkeypatch.setitem(PAGES, k, v)
    base, _ = site
    logs = []
    st = scraper.scrape(db, base + "/g/", opts(limit=1), log=logs.append)
    assert st["new"] == 1
    pid = db.execute("SELECT id FROM posts ORDER BY id DESC").fetchone()[0]
    assert [(f["w"], f["h"]) for f in core.post_files(db, pid)] == [(600, 400), (600, 400)]
    assert any("2 files skipped: same picture" in m for m in logs)


def test_app_icon_is_public_but_nothing_else(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from minibooru.web import create_app
    monkeypatch.setattr(core, "DATA", tmp_path / "d")
    monkeypatch.setenv("MINIBOORU_PASSWORD", "pw")
    with core.connect():
        pass
    cl = TestClient(create_app())
    r = cl.get("/favicon.ico")
    assert r.status_code == 200 and r.headers["content-type"] == "image/x-icon"
    assert cl.get("/brand/icon-128.png").status_code == 200
    assert cl.get("/brand/../db.sqlite", follow_redirects=False).status_code in (303, 404)
    assert 'rel="icon"' in cl.get("/login").text                         # tab icon on the login page too
    assert cl.get("/", follow_redirects=False).status_code == 303          # pages still need the login
    for sneaky in ("/static/../files/x.jpg", "/static/%2e%2e/db.sqlite"):
        assert cl.get(sneaky, follow_redirects=False).status_code in (303, 404)
    cl.post("/login", data={"password": "pw"})
    t = cl.get("/").text
    assert '<img src="/brand/icon-128.png?v=0"' in t and 'href="/favicon.ico?v=0"' in t


def test_deleted_file_is_not_downloaded_again(site, db, monkeypatch):
    from test_copies import scene
    def enc(im):
        b = BytesIO()
        im.save(b, "JPEG", quality=90)
        return b.getvalue()
    pages = {
        "/h/": ("text/html", b"<article><a href='/hpost/'><img src='/s.jpg' alt='Robin pair'></a></article>"),
        "/hpost/": ("text/html", b"<main><img src='/h1.jpg'><img src='/h2.jpg'></main>"),
        "/h1.jpg": ("image/jpeg", enc(scene(21))), "/h2.jpg": ("image/jpeg", enc(scene(22))),
    }
    for k, v in pages.items():
        monkeypatch.setitem(PAGES, k, v)
    base, _ = site
    scraper.scrape(db, base + "/h/", opts(limit=1), log=lambda m: None)
    pid = db.execute("SELECT id FROM posts ORDER BY id DESC").fetchone()[0]
    files = core.post_files(db, pid)
    assert len(files) == 2
    core.remove_file(db, pid, files[1]["sha"])
    core.delete_post(db, pid)                       # deleting the whole post allows scraping it again...
    logs = []
    scraper.scrape(db, base + "/h/", opts(limit=1), log=logs.append)
    pid2 = db.execute("SELECT id FROM posts ORDER BY id DESC").fetchone()[0]
    assert [f["sha"] for f in core.post_files(db, pid2)] == [files[0]["sha"]]   # ...but the deleted picture stays gone
    assert any("you deleted it before" in m for m in logs)


WP_POST = b"""<html><head><meta property="og:title" content="Robin Pair Photos - site"></head><body>
<article class="entry single-entry post tag-robin tag-birds">
  <nav><span><a class="bc-home" href="/">Home</a></span> / <a href="/category/news/" rel="category tag">News</a></nav>
  <span class="category-links"><a href="/category/teasers/" rel="tag">Teasers</a></span>
  <h1 class="entry-title">Robin Pair Photos (HD)</h1><time datetime="2026-11-11T13:17:17+00:00">Nov 11</time>
  <div class="gallery">
    <a class="fg-thumb" href="/full/w1.jpg"><img src="/thumbs/w1_300.jpg" width="300" height="300"></a>
    <a class="fg-thumb" href="/full/w2.jpg"><img src="/thumbs/w2_300.jpg" width="300" height="300"></a>
  </div>
  <p><a href="https://twitter.com/hashtag/ROBIN">#ROBIN</a></p>
  <div class="entry-tags"><span class="tags-links">
    <a class="tag-link" rel="tag" href="/tag/robin/"># Robin</a>
    <a class="tag-link" rel="tag" href="/tag/birds/"># Birds</a>
    <a class="tag-link" rel="tag" href="/tag/follow-us-on-google-news/"># Follow us on Google News</a>
  </span></div>
</article>
<ul class="related"><li><article class="entry loop-entry"><a href="/2019/04/07/old-post/"><img src="/thumbs/old.jpg"></a><h2>Old Robin post</h2></article></li></ul>
</body></html>"""


def test_single_post_tags_strict():
    from bs4 import BeautifulSoup
    soup = BeautifulSoup(WP_POST, "html.parser")
    labels, too_long = scraper.single_post_tags(soup.find("article"), "https://site.test/2026/11/11/robin-pair/")
    assert labels == ["Robin", "Birds"] and too_long == 1     # no Home/News/hashtag link; '#' removed; long one skipped


def test_scrape_single_post(site, db, monkeypatch):
    from test_copies import scene
    def enc(im):
        b = BytesIO()
        im.save(b, "JPEG", quality=90)
        return b.getvalue()
    for k, v in {"/2026/11/11/robin-pair/": ("text/html", WP_POST),
                 "/full/w1.jpg": ("image/jpeg", enc(scene(51))), "/full/w2.jpg": ("image/jpeg", enc(scene(52)))}.items():
        monkeypatch.setitem(PAGES, k, v)
    base, hits = site
    url = base + "/2026/11/11/robin-pair/"
    logs = []
    st = scraper.scrape_single(db, url, opts(keywords=[], single=True), log=logs.append)
    assert st["new"] == 1 and st["error"] == 0
    p = db.execute("SELECT * FROM posts ORDER BY id DESC").fetchone()
    assert p["title"] == "Robin Pair Photos (HD)" and p["source"] == url and p["posted"] == "2026-11-11 13:17:17"
    assert [(f["w"], f["h"]) for f in core.post_files(db, p["id"])] == [(600, 400), (600, 400)]   # full size, not thumbnails
    assert {(t["name"], t["category"]) for t in core.post_tags(db, p["id"])} == {("robin", "person"), ("birds", "person")}
    assert not any("/thumbs/" in h or h in ("/", "/2019/04/07/old-post/") for h in hits)   # no thumbnail, homepage or related post
    assert any("longer than 12 letters" in m for m in logs)
    st = scraper.scrape_single(db, url, opts(keywords=[], single=True), log=logs.append)
    assert st["already"] == 1 and st["new"] == 0                                # second time: already saved
    assert db.execute("SELECT keywords FROM scrape_runs ORDER BY id DESC").fetchone()[0] == "(single post)"


def test_scrape_form_single_post_needs_no_keywords(site, tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from minibooru import watcher
    from minibooru.web import create_app
    import time as _t
    monkeypatch.setattr(core, "DATA", tmp_path / "d")
    monkeypatch.setitem(PAGES, "/one/", ("text/html", b"<article><h1>Robin one</h1><img src='/big1.jpg' width='800'></article>"))
    base, _ = site
    cl = TestClient(create_app())
    assert cl.post("/scrape", data={"url": base + "/one/", "keywords": ""}, follow_redirects=False).status_code == 400  # listing still needs them
    r = cl.post("/scrape", data={"url": base + "/one/", "keywords": "", "url_kind": "single"}, follow_redirects=False)
    assert r.status_code == 303
    for _ in range(200):
        if not watcher.scrape_lock.locked():
            break
        _t.sleep(0.05)
    with core.connect() as c:
        assert c.execute("SELECT title FROM posts").fetchone()[0] == "Robin one"


@pytest.mark.parametrize("url, n, want", [
    ("https://s.test/tag/x/", 1, "https://s.test/tag/x/"),
    ("https://s.test/tag/x/", 250, "https://s.test/tag/x/page/250/"),
    ("https://s.test/tag/x", 3, "https://s.test/tag/x/page/3/"),
    ("https://s.test/tag/x/page/7/", 40, "https://s.test/tag/x/page/40/"),
    ("https://s.test/list?page=2&sort=new", 9, "https://s.test/list?page=9&sort=new"),
    ("https://s.test/list?sort=new&page=2", 9, "https://s.test/list?sort=new&page=9"),
    ("https://s.test/?s=robin", 4, "https://s.test/page/4/?s=robin"),
])
def test_listing_page_url(url, n, want):
    assert scraper.listing_page_url(url, n) == want


def test_scrape_can_start_at_a_later_page(site, db, monkeypatch):
    for k, v in {
        "/pg/": ("text/html", b"<article><a href='/post/1/'><img src='/t1.jpg' alt='Robin page one'></a></article>"),
        "/pg/page/2/": ("text/html", b"<article><a href='/post/1/'><img src='/t1.jpg' alt='Robin page two'></a></article>"),
        "/pg/page/3/": ("text/html", b"<article><a href='/post/4/'><img src='/t4.jpg' alt='Robin page three'></a></article>"),
    }.items():
        monkeypatch.setitem(PAGES, k, v)
    base, hits = site
    logs = []
    st = scraper.scrape(db, base + "/pg/", opts(limit=5, start_page=3), log=logs.append)
    assert "/pg/" not in hits and "/pg/page/2/" not in hits and "/pg/page/3/" in hits    # pages 1-2 never read
    assert st["new"] == 1 and any(m == "Scraping from page 3" for m in logs) and any("Listing page 3:" in m for m in logs)
    assert any("no more pages" in m for m in logs)                                      # page 4 is a 404: normal end
    logs.clear()
    st = scraper.scrape(db, base + "/pg/", opts(limit=5, start_page=99), log=logs.append)
    assert st["error"] == 1 and any("HTTP 404" in m for m in logs)                      # a start page that doesn't exist is an error, not "done"


def test_scrape_form_start_page(site, tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from minibooru import watcher
    from minibooru.web import create_app
    import time as _t
    monkeypatch.setattr(core, "DATA", tmp_path / "d")
    monkeypatch.setitem(PAGES, "/sp/page/2/", ("text/html", b"<article><a href='/post/4/'><img src='/t4.jpg' alt='Robin p2'></a></article>"))
    base, hits = site
    cl = TestClient(create_app())
    assert 'name="start_page"' in cl.get("/scrape").text
    r = cl.post("/scrape", data={"url": base + "/sp/", "keywords": "robin", "start_page": "2"}, follow_redirects=False)
    jid = r.headers["location"].rsplit("/", 1)[-1]
    for _ in range(200):
        if not watcher.scrape_lock.locked():
            break
        _t.sleep(0.05)
    page = cl.get(f"/scrape/{jid}").text
    assert "starting at listing page 2" in page and "/sp/page/2/" in hits and "/sp/" not in hits


def test_dropped_connection_skips_only_that_post(site, db, monkeypatch):
    """A site resetting the connection ("Connection reset by peer") costs one post, not the whole run."""
    real, calls = scraper._import_post, []
    def flaky(c, f, card, *a, **kw):
        calls.append(card.url)
        if len(calls) == 1:
            raise ConnectionResetError(104, "Connection reset by peer")
        return real(c, f, card, *a, **kw)
    monkeypatch.setattr(scraper, "_import_post", flaky)
    logs = []
    st = scraper.scrape(db, site[0] + "/list/", opts(pages=1), log=logs.append)
    assert st["error"] == 1 and st["new"] == 1 and len(calls) == 2           # skipped one, carried on
    assert any("skipped" in m and "reset" in m for m in logs)
    assert tuple(db.execute("SELECT errors, new FROM scrape_runs ORDER BY id DESC").fetchone()) == (1, 1)


def test_unexpected_error_still_lands_in_history(site, db, monkeypatch):
    def boom(*a, **kw):
        raise RuntimeError("something odd")
    monkeypatch.setattr(scraper, "_import_post", boom)
    logs = []
    st = scraper.scrape(db, site[0] + "/list/", opts(pages=1), log=logs.append)
    assert st["error"] == 1 and any("unexpected error: RuntimeError" in m for m in logs)
    row = db.execute("SELECT errors, finished, duration_s FROM scrape_runs ORDER BY id DESC").fetchone()
    assert row[0] == 1 and row[1] and row[2] is not None                       # recorded with its end time


def test_cut_off_download_is_retried(tmp_path, monkeypatch):
    f = scraper.Fetcher(opts(), lambda m: None)
    monkeypatch.setattr(f, "_sleep", lambda s: None)
    tries = []
    def once(url, d):
        tries.append(url)
        if len(tries) < 3:
            raise ConnectionResetError(104, "Connection reset by peer")
        return d / "dl.jpg"
    monkeypatch.setattr(f, "_download_once", once)
    assert f.download("https://site.example/a.jpg", tmp_path) == tmp_path / "dl.jpg" and len(tries) == 3
    tries.clear()
    monkeypatch.setattr(f, "_download_once", lambda u, d: (_ for _ in ()).throw(ConnectionResetError(104, "reset")))
    with pytest.raises(scraper.ScrapeError, match="cut off"):
        f.download("https://site.example/a.jpg", tmp_path)
    f.client.close()


def test_connection_blip_mid_run_is_retried_but_offline_fails_fast(monkeypatch):
    calls = []
    def handler(req):
        calls.append(1)
        if len(calls) in (2, 3):                                  # reset while connecting, twice
            raise httpx.ConnectError("[Errno 104] Connection reset by peer")
        return httpx.Response(200, text="ok")
    f = scraper.Fetcher(opts(allow_private=True), lambda m: None)
    f.client = httpx.Client(transport=httpx.MockTransport(handler))
    monkeypatch.setattr(f, "_sleep", lambda s: None)
    assert f._request("https://site.example/a").status_code == 200         # first request works
    assert f._request("https://site.example/b").status_code == 200 and len(calls) == 4   # blip retried, not fatal
    g = scraper.Fetcher(opts(allow_private=True), lambda m: None)
    g.client = httpx.Client(transport=httpx.MockTransport(lambda req: (_ for _ in ()).throw(httpx.ConnectError("down"))))
    with pytest.raises(httpx.ConnectError):                                  # never reached the site: offline, fail fast
        g._request("https://site.example/a")


def test_run_log_tracks_current_page():
    log = scraper.RunLog()
    assert log.page is None
    log.append("Scraping from page 40")
    assert log.page == 40
    log.append("Listing page 41: https://example.com/page/41/")
    log.append("  found 3 new post cards")
    assert log.page == 41


def _wait_idle():
    from minibooru import watcher
    import time as _t
    for _ in range(300):
        if not watcher.scrape_lock.locked():
            return
        _t.sleep(0.05)


def test_unfinished_run_continues_after_a_restart(site, tmp_path, monkeypatch):
    import json
    from fastapi.testclient import TestClient
    from minibooru.web import create_app
    monkeypatch.setattr(core, "DATA", tmp_path / "d")
    monkeypatch.setitem(PAGES, "/rs/page/2/", ("text/html", b"<article><a href='/post/4/'><img src='/t4.jpg' alt='Robin p2'></a></article>"))
    base, hits = site
    spec = {"url": base + "/rs/", "keywords": ["robin"], "single": False, "limit": 1_000_000, "until": "2000-01-01",
            "stop_text": "posts dated before Jan 1, 2000", "card": "", "title": "", "tag": "", "media": "", "date": "", "start_page": 1}
    with core.connect() as c:      # what a run leaves behind when the app is stopped part-way (it had reached page 2)
        core.set_setting(c, "unfinished_run", json.dumps({"spec": spec, "started": "2026-01-01 00:00:00", "resumes": 0, "page": 2}))
    app = create_app()
    app.state.resume_unfinished(0)
    _wait_idle()
    assert "/rs/page/2/" in hits and "/rs/" not in hits                         # carried on at page 2, page 1 not re-read
    with core.connect() as c:
        assert core.get_setting(c, "unfinished_run") is None                      # finished normally: nothing left to resume
    page = TestClient(app).get("/scrape").text
    assert page.count("<tr") >= 2                                                # the resumed run is in the history


def test_run_saves_its_page_and_gives_up_after_repeated_restarts(tmp_path, monkeypatch):
    import json
    from minibooru.web import create_app
    monkeypatch.setattr(core, "DATA", tmp_path / "d")
    seen = {}
    def fake_scrape(c, url, opt, log):
        log("Listing page 7: " + url + "page/7/")
        with core.connect() as c2:
            seen["state"] = json.loads(core.get_setting(c2, "unfinished_run"))
        return {}
    monkeypatch.setattr(scraper, "scrape", fake_scrape)
    from fastapi.testclient import TestClient
    app = create_app()
    TestClient(app).post("/scrape", data={"url": "https://example.com/l/", "keywords": "robin", "stop_mode": "date", "until": "2020-01-01"})
    _wait_idle()
    assert seen["state"]["page"] == 7 and seen["state"]["spec"]["until"] == "2020-01-01"   # saved while running
    spec = seen["state"]["spec"]
    with core.connect() as c:      # it already failed at page 7 three times in a row: don't try again
        core.set_setting(c, "unfinished_run", json.dumps({"spec": spec, "started": "x", "resumes": 3, "page": 7, "last_resume_page": 7}))
    seen.clear()
    app.state.resume_unfinished(0)
    _wait_idle()
    with core.connect() as c:
        assert core.get_setting(c, "unfinished_run") is None and not seen

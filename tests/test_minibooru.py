import pytest
from PIL import Image

from minibooru import core


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(core, "DATA", tmp_path / "data")
    return tmp_path


def img(path, color):
    Image.new("RGB", (40, 30), color).save(path)
    return path


@pytest.mark.parametrize("raw,want", [
    ("Barn Swallow", "barn_swallow"), ("  Robin  ", "robin"), ("a:b,c", "a_b_c"),
    ("-bird", "bird"), ("직캠", "직캠"), ("x   y", "x_y")])
def test_normalize(raw, want):
    assert core.normalize_tag(raw) == want


@pytest.mark.parametrize("text,kws,want", [
    ("Robin on a branch", ["robin"], ["robin"]),
    ("Robinson crusoe", ["robin"], []),                   # word boundary
    ("Dance practice + selfie", ["dance", "selfie"], ["dance", "selfie"]),
    ("오늘의 직캠 영상", ["직캠"], ["직캠"]),             # Korean substring
    ("nothing here", ["dance"], [])])
def test_keyword_match(text, kws, want):
    assert core.keyword_match(text, kws) == want


def test_parse_query():
    assert core.parse_query("a -b type:video c") == (["a", "c"], ["b"], {"type": "video"})


def test_ingest_dedupe_and_search(env):
    with core.connect() as c:
        a, st = core.ingest(c, img(env / "a.png", "red"), ["Robin", ("Sam Smith", "person")])
        assert st == "new"
        a2, st2 = core.ingest(c, env / "a.png", ["morning"])
        assert (a2, st2) == (a, "duplicate")                   # same checksum -> merged
        b, _ = core.ingest(c, img(env / "b.png", "blue"), ["robin", "night"])
        rows, total = core.search(c, "robin")
        assert total == 2
        assert [r["id"] for r in core.search(c, "robin -night")[0]] == [a]
        assert core.search(c, "unknown_tag") == ([], 0)
        assert {t["name"] for t in core.post_tags(c, a)} == {"robin", "sam_smith", "morning"}


def test_alias_and_implication(env):
    with core.connect() as c:
        core.add_implication(c, "robin", "bird")
        core.add_alias(c, "european robin", "robin")
        p, _ = core.ingest(c, img(env / "a.png", "red"), ["European Robin"])
        assert {t["name"] for t in core.post_tags(c, p)} == {"robin", "bird"}
        assert core.search(c, "european_robin")[1] == 1        # alias works in search
        with pytest.raises(ValueError):
            core.add_implication(c, "bird", "robin")           # loop rejected


def test_rejects_non_media(env):
    (env / "x.txt").write_text("hi")
    with core.connect() as c, pytest.raises(ValueError):
        core.ingest(c, env / "x.txt")


def test_web_flow(env, monkeypatch):
    from fastapi.testclient import TestClient
    from minibooru.web import create_app
    monkeypatch.setenv("MINIBOORU_PASSWORD", "pw")
    cl = TestClient(create_app())
    r = cl.get("/", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].startswith("/login")
    assert cl.get("/files/x/none.png", follow_redirects=False).status_code == 303   # files need login too
    assert cl.post("/post/1/favorite", follow_redirects=False).status_code == 401
    assert cl.post("/login", data={"password": "nope"}).status_code == 401
    r = cl.post("/login", data={"password": "pw", "next": "//evil.com"}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/"                    # no open redirect
    assert cl.get("/").status_code == 200                                           # cookie now set
    p = img(env / "u.png", "green")
    r = cl.post("/upload", files={"files": ("u.png", p.read_bytes(), "image/png")},
                data={"tags": "Gull sea_bird"}, follow_redirects=False)
    assert r.status_code == 303
    assert "1 post" in cl.get("/?q=gull").text
    assert cl.get("/post/1").status_code == 200
    assert cl.get("/tags").status_code == 200
    assert cl.get("/files/x/none.png").status_code == 404


@pytest.mark.parametrize("name,ok", [("robin", True), ("Blue Jay", True), ("2024", True),
                                     ("로빈", False), ("Robin (로빈)", False), ("café", False), ("画像", False)])
def test_is_english_tag(name, ok):
    assert core.is_english_tag(name) is ok


def test_english_tags_filters_pairs():
    kept, dropped = core.english_tags(["dance", ("직캠", "meta"), ("fancam", "meta")])
    assert kept == ["dance", ("fancam", "meta")] and dropped == 1


def test_favorite_filters(env):
    with core.connect() as c:
        a, _ = core.ingest(c, img(env / "a.png", "red"), ["x"])
        v = c.execute("INSERT INTO posts(sha256, ext, mime) VALUES ('v','mp4','video/mp4')").lastrowid
        c.execute("UPDATE posts SET favorite=1 WHERE id IN (?,?)", (a, v))
        b, _ = core.ingest(c, img(env / "b.png", "blue"), ["x"])
        ids = lambda view: {r["id"] for r in core.search(c, "", view=view)[0]}
        assert ids("videos") == {v} and ids("images") == {a, b}
        assert ids("fav") == {a, v} and ids("fav_videos") == {v} and ids("fav_images") == {a}
        assert ids("all") == {a, b, v}


def test_multi_media_post(env):
    with core.connect() as c:
        pid, st = core.ingest(c, img(env / "a.png", "red"), ["x"])
        assert core.add_media(c, pid, img(env / "b.png", "blue")) is True
        assert core.add_media(c, pid, env / "b.png") is False          # same file twice
        assert core.add_media(c, pid, env / "a.png") is False          # primary file again
        files = core.post_files(c, pid)
        assert len(files) == 2 and all(f["is_image"] for f in files)
        assert core.delete_post(c, pid)
        assert not list((core.DATA / "files").rglob("*.png"))


def scene(path, seed, size=(640, 480), fmt=None, quality=95):
    """A structured picture (random shapes) so it has a real fingerprint."""
    import random
    from PIL import ImageDraw
    rnd = random.Random(seed)
    im = Image.new("RGB", (640, 480), (rnd.randrange(256),) * 3)
    d = ImageDraw.Draw(im)
    for _ in range(25):
        x, y = rnd.randrange(600), rnd.randrange(440)
        d.rectangle([x, y, x + rnd.randrange(20, 200), y + rnd.randrange(20, 200)], fill=tuple(rnd.randrange(256) for _ in range(3)))
    if size != (640, 480):
        im = im.resize(size)
    im.save(path, fmt, quality=quality) if fmt == "JPEG" else im.save(path)
    return path


def test_similar_images_flagged(env):
    with core.connect() as c:
        a, _ = core.ingest(c, scene(env / "web.png", 1), ["x"])
        b, st = core.ingest(c, scene(env / "phone.jpg", 1, size=(320, 240), fmt="JPEG", quality=40), ["y"])   # same pic, smaller + recompressed
        assert st == "new"
        o, _ = core.ingest(c, scene(env / "other.png", 2), ["z"])                                          # different pic
        flags = c.execute("SELECT a, b FROM dup_flags WHERE status='open'").fetchall()
        assert [tuple(f) for f in flags] == [(a, b)]
        assert {r["id"] for r in core.search(c, "", view="dups")[0]} == {a, b}
        core.delete_post(c, a)
        assert c.execute("SELECT COUNT(*) FROM dup_flags").fetchone()[0] == 0     # flag goes with the post


def test_plain_images_not_flagged(env):
    with core.connect() as c:
        core.ingest(c, img(env / "r.png", "red"), [])
        core.ingest(c, img(env / "b.png", "blue"), [])                  # flat colours must not match each other
        assert c.execute("SELECT COUNT(*) FROM dup_flags").fetchone()[0] == 0


def test_similar_video_flagged(env):
    import shutil, subprocess
    if not shutil.which("ffmpeg"):
        pytest.skip("no ffmpeg")
    src, copy = env / "a.mp4", env / "b.mp4"
    subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "testsrc2=size=320x240:rate=15:duration=6", "-pix_fmt", "yuv420p", str(src)], check=True)
    subprocess.run(["ffmpeg", "-v", "error", "-i", str(src), "-vf", "scale=160:120", "-crf", "35", "-pix_fmt", "yuv420p", str(copy)], check=True)
    with core.connect() as c:
        a, _ = core.ingest(c, src, [])
        b, _ = core.ingest(c, copy, [])
        assert [tuple(r) for r in c.execute("SELECT a, b FROM dup_flags")] == [(a, b)]


def test_dup_buttons_and_backfill(env, monkeypatch):
    from fastapi.testclient import TestClient
    from minibooru import similar
    from minibooru.web import create_app
    monkeypatch.delenv("MINIBOORU_PASSWORD", raising=False)
    with core.connect() as c:
        a, _ = core.ingest(c, scene(env / "w.png", 3), ["site_tag"])
        b, _ = core.ingest(c, scene(env / "p.jpg", 3, size=(320, 240), fmt="JPEG", quality=50), ["mine"])
    cl = TestClient(create_app())
    assert "Possible duplicate" in cl.get(f"/post/{b}").text and "dup</span>" in cl.get("/").text
    cl.post(f"/post/{b}/dup/{a}/merge", follow_redirects=False)
    with core.connect() as c:
        assert c.execute("SELECT id FROM posts").fetchall()[0][0] == a
        assert {t["name"] for t in core.post_tags(c, a)} == {"site_tag", "mine"}
    # old posts without fingerprints get checked by backfill
    with core.connect() as c:
        c.execute("DELETE FROM fingerprints")
        core.ingest(c, scene(env / "w2.jpg", 3, size=(200, 150), fmt="JPEG"), [])
        c.execute("DELETE FROM dup_flags"); c.execute("DELETE FROM fingerprints")
    assert similar.backfill(core.connect, core.DATA) == 2
    with core.connect() as c:
        assert c.execute("SELECT COUNT(*) FROM dup_flags").fetchone()[0] == 1
        c.execute("UPDATE dup_flags SET status='dismissed'")
    cl.post("/post/1/dup/2/dismiss")
    assert "Possible duplicate" not in cl.get("/post/1").text


def test_file_count_in_search(env):
    with core.connect() as c:
        a, _ = core.ingest(c, scene(env / "1.png", 10), [])
        core.add_media(c, a, scene(env / "2.png", 11))
        core.add_media(c, a, scene(env / "3.png", 12))
        b, _ = core.ingest(c, scene(env / "4.png", 13), [])
        n = {r["id"]: r["nfiles"] for r in core.search(c, "")[0]}
        assert n == {a: 3, b: 1}


def test_auto_rescan_button_and_busy_message(env, monkeypatch):
    from fastapi.testclient import TestClient
    from minibooru import watcher
    from minibooru.web import create_app
    monkeypatch.delenv("MINIBOORU_PASSWORD", raising=False)
    cl = TestClient(create_app())
    form = {"url": "https://example.com/list/", "keywords": "robin\nwren", "limit": "5", "max_posts": "7", "interval_min": "30"}
    assert cl.post("/watches", data=form, follow_redirects=False).status_code == 303   # same fields as the scrape form
    with core.connect() as c:
        w = c.execute("SELECT * FROM watches").fetchone()
        assert (w["max_posts"], w["interval_min"]) == (7, 30)
    page = cl.get("/scrape").text
    assert "▶ Start auto rescan" in page and "up to 7 new" in page
    assert watcher.scrape_lock.acquire(blocking=False)
    watcher.current = "auto rescan of https://example.com/list/"
    try:
        r = cl.post("/scrape", data={"url": "https://example.com/", "keywords": "x"})
        assert r.status_code == 409 and "auto rescan of https://example.com/list/" in r.text
        assert "Running now" in cl.get("/scrape").text
    finally:
        watcher.current = None
        watcher.scrape_lock.release()


def test_parse_user_date():
    from datetime import date
    from minibooru.scraper import parse_user_date as p
    assert p("03/15/2024") == p("15/03/2024") == p("2024-03-15") == p("Mar 15, 2024") == p("15th March 2024") == date(2024, 3, 15)
    assert p("03/04/2024") == date(2024, 3, 4)                 # ambiguous: month first
    assert p("3/4/24") == date(2024, 3, 4) and p("20240304") == date(2024, 3, 4)
    assert p("garbage") is None and p("13/13/2024") is None and p("") is None


def test_watch_controls_and_date_validation(env, monkeypatch):
    from fastapi.testclient import TestClient
    from minibooru import watcher
    from minibooru.web import create_app
    monkeypatch.delenv("MINIBOORU_PASSWORD", raising=False)
    cl = TestClient(create_app())
    base = {"url": "https://example.com/l/", "keywords": "robin", "max_posts": "20", "interval_min": "30"}
    r = cl.post("/watches", data={**base, "w_mode": "date", "w_until": "not a date"})
    assert r.status_code == 400 and "Could not read" in r.text and "<html" in r.text          # friendly page, not JSON
    assert cl.post("/watches", data={**base, "w_mode": "date", "w_until": "01/01/2999"}).status_code == 400
    assert cl.post("/watches", data={**base, "w_mode": "date", "w_until": "03/15/2024"}, follow_redirects=False).status_code == 303
    page = cl.get("/scrape").text
    assert "back to March 15, 2024" in page and "Next run in" in page or "Queued" in page
    cl.post("/watches/1/edit", data={"w_mode": "count", "max_posts": "9", "interval_min": "45"})
    with core.connect() as c:
        w = c.execute("SELECT * FROM watches").fetchone()
        assert (w["max_posts"], w["until"], w["interval_min"]) == (9, "", 45)
    cl.post("/watches/1/toggle")
    assert "Paused" in cl.get("/scrape/watches").text
    # a running watch shows Stop; pressing it sets the cancel flag
    import threading
    watcher.running_wid, watcher.cancel, watcher.current = 1, threading.Event(), "auto rescan of https://example.com/l/"
    try:
        frag = cl.get("/scrape/watches").text
        assert "Running now" in frag and "/watches/1/stop" in frag
        watcher.logs[1] = ["hello log"]
        assert "hello log" in cl.get("/watch/1").text
        cl.post("/watches/1/stop")
        assert watcher.cancel.is_set()
    finally:
        watcher.running_wid = watcher.cancel = watcher.current = None
    cl.post("/watches/1/delete")
    assert cl.get("/watch/1").status_code == 404


def test_banner_links_and_stop_for_manual_scrape(env, monkeypatch):
    import threading
    from fastapi.testclient import TestClient
    from minibooru import watcher
    from minibooru.web import create_app
    monkeypatch.delenv("MINIBOORU_PASSWORD", raising=False)
    cl = TestClient(create_app())
    watcher.current, watcher.current_jid = "manual scrape of https://example.com/", "abc12345"
    try:
        page = cl.get("/scrape").text
        assert 'href="/scrape/abc12345"' in page and 'data-href="/scrape/abc12345"' in page
        assert 'action="/scrape/abc12345/stop"' in page and 'name="next" value="/scrape"' in page   # Stop button on the Scrape page
    finally:
        watcher.current = watcher.current_jid = None


def test_tags_page_one_section_per_type(env, monkeypatch):
    from fastapi.testclient import TestClient
    from minibooru.web import create_app
    monkeypatch.delenv("MINIBOORU_PASSWORD", raising=False)
    with core.connect() as c:
        core.set_tag_types(c, ["person", "artist"])
        pid = c.execute("INSERT INTO posts(sha256, ext, mime) VALUES ('ab','png','image/png')").lastrowid
        core.add_tags(c, pid, [("robin", "person"), ("bob_photo", "artist"), ("fancam", "other"), ("misc", None)])
    cl = TestClient(create_app())
    page = cl.get("/tags").text
    i, cr, ot = page.index("●</span> Person"), page.index("●</span> Artist"), page.index("Other tags (2)")
    assert i < cr < ot                                                          # in the order the types were made, other hidden last
    assert "robin" in page[i:cr] and "bob_photo" in page[cr:ot] and "fancam" in page[ot:] and "misc" in page[ot:]
    assert "fancam" not in page[:ot]
    cl.post("/tags/bulk-category", data={"names": ["misc"], "category": "artist"})
    with core.connect() as c:
        assert c.execute("SELECT category FROM tags WHERE name='misc'").fetchone()[0] == "artist"
    assert cl.post("/tags/bulk-category", data={"names": ["misc"], "category": "species"}).status_code == 400


def test_new_install_has_only_other(env, monkeypatch):
    from fastapi.testclient import TestClient
    from minibooru.web import create_app
    monkeypatch.delenv("MINIBOORU_PASSWORD", raising=False)
    with core.connect() as c:
        assert core.tag_types(c) == [("other", core.OTHER_COLOR)] and core.tag_rule(c) == ("other",) * 3
        pid = c.execute("INSERT INTO posts(sha256, ext, mime) VALUES ('ab','png','image/png')").lastrowid
        core.add_tags(c, pid, [("robin", "idol"), ("wren", "creator")])          # types this install doesn't have
        assert {r[0] for r in c.execute("SELECT DISTINCT category FROM tags")} == {"other"}
    page = TestClient(create_app()).get("/tags").text
    assert "<h3>All tags" in page and "Other tags" not in page and "idol" not in page.lower()


def test_tag_types_add_rename_delete(env, monkeypatch):
    from fastapi.testclient import TestClient
    from minibooru.web import create_app
    monkeypatch.delenv("MINIBOORU_PASSWORD", raising=False)
    cl = TestClient(create_app())
    assert cl.post("/tags/types/add", data={"name": "Person"}, follow_redirects=False).headers["location"] == "/tags#types"
    cl.post("/tags/types/add", data={"name": "artist", "color": "#123456"})
    assert cl.post("/tags/types/add", data={"name": "person"}).status_code == 400          # already there
    assert cl.post("/tags/types/add", data={"name": "no/slash"}).status_code == 400
    with core.connect() as c:
        assert core.type_names(c) == ["person", "artist", "other"]
        assert dict(core.tag_types(c))["artist"] == "#123456"
        pid = c.execute("INSERT INTO posts(sha256, ext, mime) VALUES ('ab','png','image/png')").lastrowid
        core.add_tags(c, pid, [("robin", "person"), ("bob", "artist")])
    cl.post("/tags/rule", data={"first": "artist", "middle": "person", "last": "other"})
    cl.post("/tags/types/edit", data={"name": "person", "new_name": "bird", "color": "#00ff00"})
    with core.connect() as c:
        assert core.tag_rule(c) == ("artist", "bird", "other")                      # the rule follows a rename
        assert c.execute("SELECT category FROM tags WHERE name='robin'").fetchone()[0] == "bird"
        assert dict(core.tag_types(c))["bird"] == "#00ff00"
    assert cl.post("/tags/types/edit", data={"name": "other", "new_name": "misc"}).status_code == 400
    assert cl.post("/tags/types/delete", data={"name": "other"}).status_code == 400
    cl.post("/tags/types/delete", data={"name": "artist"})
    with core.connect() as c:
        assert core.type_names(c) == ["bird", "other"]
        assert c.execute("SELECT category FROM tags WHERE name='bob'").fetchone()[0] == "other"   # moved, not lost
        assert core.tag_rule(c) == ("other", "bird", "other")
    page = cl.get("/tags").text
    assert 'id="types"' in page and 'value="bird"' in page


def test_old_categories_are_folded_into_three_types(env):
    """Databases from before tag types could be chosen keep idol / creator / other and their position rule."""
    import sqlite3
    with core.connect() as c:
        pass
    raw = sqlite3.connect(core.DATA / "db.sqlite")                 # simulate an old database
    raw.execute("PRAGMA foreign_keys=OFF")
    for cat in ("creators", "idols", "group", "species", "general"):
        raw.execute("INSERT OR IGNORE INTO categories(name, color) VALUES (?, '#999')", (cat,))
    for name, cat in (("bob", "creators"), ("jane", "idols"), ("lsf", "group"), ("robin", "species"), ("x", "general")):
        raw.execute("INSERT INTO tags(name, category) VALUES (?, ?)", (name, cat))
    raw.execute("PRAGMA user_version = 0")
    raw.commit(); raw.close()
    with core.connect() as c:
        cats = dict(c.execute("SELECT name, category FROM tags").fetchall())
        assert cats == {"bob": "creator", "jane": "idol", "lsf": "idol", "robin": "other", "x": "other"}
        assert dict(core.tag_types(c)) == dict(core.LEGACY_TYPES)
        assert core.type_names(c) == ["idol", "creator", "other"] and core.tag_rule(c) == ("creator", "idol", "other")


def test_existing_install_keeps_its_types_and_rule(env):
    """A database at version 3 (what older installs run) is left as it was, plus the rule it always used."""
    import sqlite3
    with core.connect() as c:
        pass
    raw = sqlite3.connect(core.DATA / "db.sqlite")
    raw.executemany("INSERT OR IGNORE INTO categories(name, color) VALUES (?, ?)", core.LEGACY_TYPES)
    raw.execute("INSERT INTO tags(name, category) VALUES ('jane', 'idol')")
    raw.execute("PRAGMA user_version = 3")
    raw.commit(); raw.close()
    with core.connect() as c:
        assert core.type_names(c) == ["idol", "creator", "other"] and core.tag_rule(c) == ("creator", "idol", "other")
        assert c.execute("SELECT category FROM tags WHERE name='jane'").fetchone()[0] == "idol"
        assert c.execute("PRAGMA user_version").fetchone()[0] == core.LATEST_DB_VERSION


def test_tag_type_upgrades_but_never_downgrades(env):
    with core.connect() as c:
        core.set_tag_types(c, ["person"])
        t = core.get_or_create_tag(c, "robin", "other")
        core.get_or_create_tag(c, "robin", "person")              # seen as a person later: upgraded
        core.get_or_create_tag(c, "robin", "other")               # last tag of another post: stays a person
        assert c.execute("SELECT category FROM tags WHERE id=?", (t,)).fetchone()[0] == "person"


def test_neighbors_follow_list(env):
    with core.connect() as c:
        a, _ = core.ingest(c, img(env / "a.png", "red"), ["robin"])
        b, _ = core.ingest(c, img(env / "b.png", "blue"), ["gull"])
        d, _ = core.ingest(c, img(env / "d.png", "green"), ["robin"])
        # newest first (same import second -> id breaks the tie): d, b, a
        assert core.neighbors(c, b) == (d, a)
        assert core.neighbors(c, d) == (None, b)
        assert core.neighbors(c, a) == (b, None)
        assert core.neighbors(c, b, sort="old") == (a, d)
        assert core.neighbors(c, a, q="robin") == (d, None)          # gull is skipped inside the robin list
        assert core.neighbors(c, a, q="nosuchtag") == (None, None)
        assert core.neighbors(c, 999) == (None, None)


def test_post_nav_and_page_jump(env):
    from fastapi.testclient import TestClient
    from minibooru.web import create_app, search_page
    cl = TestClient(create_app())
    with core.connect() as c:
        a, _ = core.ingest(c, img(env / "a.png", "red"), ["robin"])
        b, _ = core.ingest(c, img(env / "b.png", "blue"), ["robin"])
        _, _, page, pages = search_page(c, "", 99, "new", "all", per=1)
        assert (page, pages) == (2, 2)                               # past the end -> last page
        assert search_page(c, "", -3, "new", "all", per=1)[2] == 1
    html = cl.get(f"/post/{b}?q=robin&sort=old").text
    assert f'href="/post/{a}?q=robin&amp;sort=old" id="nav-prev"' in html
    assert 'id="nav-next"' not in html
    assert f'href="/post/{a}?q=robin"' in cl.get("/?q=robin").text   # grid links carry the list context
    assert cl.get("/?page=50").status_code == 200


def test_uploads_get_tag_and_source_filter(env):
    from fastapi.testclient import TestClient
    from minibooru.web import create_app
    cl = TestClient(create_app())
    with core.connect() as c:
        s, _ = core.ingest(c, img(env / "s.png", "red"), ["robin"], source="https://site/p/1")
        assert core.search(c, "", src="upload") == ([], 0)                 # no uploads yet
        assert core.search(c, "", src="scraped")[1] == 1
    p = img(env / "u.png", "blue")
    cl.post("/upload", files={"files": ("u.png", p.read_bytes(), "image/png")}, data={"tags": "robin"})
    with core.connect() as c:
        u = c.execute("SELECT id FROM posts WHERE source='upload'").fetchone()[0]
        assert "upload" in {t["name"] for t in core.post_tags(c, u)}
        assert [r["id"] for r in core.search(c, "", src="upload")[0]] == [u]
        assert [r["id"] for r in core.search(c, "robin", src="scraped")[0]] == [s]
        assert core.neighbors(c, u, src="upload") == (None, None)          # Prev/Next stay inside the uploads list
    html = cl.get("/?src=upload").text
    assert '<option value="upload" selected>' in html and f'href="/post/{u}?src=upload"' in html


def test_old_uploads_backfilled_with_tag(env):
    with core.connect() as c:
        pid, _ = core.ingest(c, img(env / "a.png", "red"), [], source="upload")
        c.execute("PRAGMA user_version = 2")                               # as if created before the upload tag existed
    with core.connect() as c:
        assert "upload" in {t["name"] for t in core.post_tags(c, pid)}


def test_tags_grouped_by_first_letter_with_popup_actions(env, monkeypatch):
    from fastapi.testclient import TestClient
    from minibooru.web import create_app, letter_groups
    monkeypatch.delenv("MINIBOORU_PASSWORD", raising=False)
    names = ["beta", "alpha", "apple", "9lives", "(odd)", "zed"]
    assert [(k, label, [r["name"] for r in g]) for k, label, g in letter_groups([{"name": n} for n in sorted(names)])] == \
        [("a", "A", ["alpha", "apple"]), ("b", "B", ["beta"]), ("z", "Z", ["zed"]), ("9", "9", ["9lives"]), ("etc", "#", ["(odd)"])]
    with core.connect() as c:
        pid = c.execute("INSERT INTO posts(sha256, ext, mime) VALUES ('ab','png','image/png')").lastrowid
        core.set_tag_types(c, ["person", "artist"])
        core.add_tags(c, pid, [(n, "person") for n in names])
    cl = TestClient(create_app())
    page = cl.get("/tags").text
    for sec in ("person-a", "person-b", "person-z", "person-9", "person-etc"):
        assert f'id="{sec}"' in page and f'href="#{sec}"' in page
    assert 'id="person-c"' not in page                                          # empty letters are not shown
    assert page.index('id="person-a"') < page.index('id="person-z"') < page.index('id="person-9"') < page.index('id="person-etc"')
    assert 'data-name="apple" data-type="person" data-n="1"' in page
    assert page.count('class="ghost selall" data-on="1">Select all 6<') == 1    # one per type that has tags
    assert page.count('class="ghost lsel"') == 5                                # one per letter row
    r = cl.post("/tags/category", data={"name": "apple", "category": "artist", "next": "/tags#person-a"}, follow_redirects=False)
    assert r.headers["location"] == "/tags#person-a"                            # popup returns to the same letter
    r = cl.post("/tag/beta/delete", data={"next": "/tags#person-b"}, follow_redirects=False)
    assert r.headers["location"] == "/tags#person-b"
    r = cl.post("/tag/zed/delete", data={"next": "//evil.example"}, follow_redirects=False)
    assert r.headers["location"] == "/tags"                                   # no open redirect
    with core.connect() as c:
        assert c.execute("SELECT category FROM tags WHERE name='apple'").fetchone()[0] == "artist"
        assert core.lookup_tag(c, "beta") is None


def _pic(path, color, size=(64, 48)):
    from PIL import Image, ImageDraw
    im = Image.new("RGB", size, color)
    ImageDraw.Draw(im).rectangle([5, 5, 30, 30], fill="white")
    im.save(path)
    return path


def test_remove_one_file_from_a_post_and_remember_it(env, tmp_path):
    with core.connect() as c:
        pid, _ = core.ingest(c, _pic(tmp_path / "a.png", "red"), ["x"])
        core.add_media(c, pid, _pic(tmp_path / "b.png", "blue"))
        core.add_media(c, pid, _pic(tmp_path / "c.png", "green"))
        a, b, cc = [f["sha"] for f in core.post_files(c, pid)]
        core.remove_file(c, pid, b)                                 # a middle file
        assert [f["sha"] for f in core.post_files(c, pid)] == [a, cc]
        core.remove_file(c, pid, a)                                 # the main file: the next one takes its place
        assert [f["sha"] for f in core.post_files(c, pid)] == [cc]
        assert c.execute("SELECT sha256 FROM posts WHERE id=?", (pid,)).fetchone()[0] == cc
        assert core.is_deleted_file(c, a) and core.is_deleted_file(c, b) and not core.is_deleted_file(c, cc)
        assert not list((core.DATA / "files").rglob(f"{a}.*"))   # file removed from disk
        with pytest.raises(ValueError, match="only file"):
            core.remove_file(c, pid, cc)
        with pytest.raises(ValueError, match="not part"):
            core.remove_file(c, pid, a)


def test_backgrounds_desktop_and_phone(env, tmp_path, monkeypatch):
    from io import BytesIO
    from fastapi.testclient import TestClient
    from PIL import Image
    from minibooru.web import create_app
    monkeypatch.delenv("MINIBOORU_PASSWORD", raising=False)
    cl = TestClient(create_app())
    page = cl.get("/").text
    assert "--bgimg:none" in page                                   # nothing set yet
    buf = BytesIO()
    Image.new("RGB", (300, 200), "purple").save(buf, "PNG")
    r = cl.post("/background/desktop", files={"file": ("x.png", buf.getvalue(), "image/png")})
    assert r.status_code == 200 and set(r.json()["versions"]) == {"desktop"}
    page = cl.get("/").text
    assert 'url("/background/desktop?v=' in page and "@media (max-width:760px){:root{--bgimg:none}}" in page
    import re
    start = page.index('<style id="bgcss">')
    end = page.index("</style>", start)
    assert not re.search(r"--bgimg\s*:\s*none", page[:start] + page[end:])   # nothing later overrides the picture
    got = cl.get("/background/desktop")
    assert got.status_code == 200 and got.headers["content-type"] == "image/jpeg"
    assert cl.post("/background/mobile", files={"file": ("x.png", b"not a picture", "image/png")}).status_code == 400
    assert cl.post("/background/tablet", files={"file": ("x.png", buf.getvalue(), "image/png")}).status_code == 400
    assert cl.get("/background/mobile").status_code == 404
    cl.post("/background/mobile", files={"file": ("x.png", buf.getvalue(), "image/png")})
    assert 'url("/background/mobile?v=' in cl.get("/").text
    assert cl.post("/background/desktop/clear").json()["versions"] == {"mobile": core.background_versions()["mobile"]}


def test_post_page_has_per_file_delete_only_for_multi_file_posts(env, tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from minibooru.web import create_app
    monkeypatch.delenv("MINIBOORU_PASSWORD", raising=False)
    with core.connect() as c:
        one, _ = core.ingest(c, _pic(tmp_path / "s.png", "red"), ["x"])
        many, _ = core.ingest(c, _pic(tmp_path / "m1.png", "blue"), ["x"])
        core.add_media(c, many, _pic(tmp_path / "m2.png", "green"))
        sha2 = core.post_files(c, many)[1]["sha"]
    cl = TestClient(create_app())
    assert "/file/" not in cl.get(f"/post/{one}").text
    assert f'action="/post/{many}/file/{sha2}/delete"' in cl.get(f"/post/{many}").text
    r = cl.post(f"/post/{many}/file/{sha2}/delete", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == f"/post/{many}"
    assert cl.post(f"/post/{one}/file/nope/delete").status_code == 400


def test_upload_several_files_as_one_post(env, tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from minibooru.web import create_app
    monkeypatch.delenv("MINIBOORU_PASSWORD", raising=False)
    cl = TestClient(create_app())
    files = [("files", (f"{c}.png", _pic(tmp_path / f"{c}.png", c).read_bytes(), "image/png")) for c in ("red", "blue", "green")]
    r = cl.post("/upload", data={"tags": "x", "together": "1"}, files=files, follow_redirects=False)
    pid = int(r.headers["location"].rsplit("/", 1)[-1])
    with core.connect() as c:
        assert len(core.post_files(c, pid)) == 3 and c.execute("SELECT COUNT(*) FROM posts").fetchone()[0] == 1
    cl.post("/upload", data={"tags": "y"}, files=[("files", ("o.png", _pic(tmp_path / "o.png", "orange").read_bytes(), "image/png")),
                                                  ("files", ("p.png", _pic(tmp_path / "p.png", "purple").read_bytes(), "image/png"))])
    with core.connect() as c:
        assert c.execute("SELECT COUNT(*) FROM posts").fetchone()[0] == 3        # without the box: one post per file


def test_background_editor_is_shared(env, tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from minibooru.web import create_app
    monkeypatch.delenv("MINIBOORU_PASSWORD", raising=False)
    with core.connect() as c:
        pid, _ = core.ingest(c, _pic(tmp_path / "w.png", "red", (300, 150)), ["x"])
    cl = TestClient(create_app())
    for path in ("/", f"/post/{pid}", "/tags"):
        page = cl.get(path).text
        assert page.count('id="bged"') == 1 and 'value="phone">Phone screen' in page    # one editor, with the phone shape
    post = cl.get(f"/post/{pid}").text
    assert 'id="bg-img"' not in post and "mbBgEditor.open(" in post                     # no second copy on the post page
    assert "mbBgEditor.open(URL.createObjectURL(f)" in cl.get("/").text                 # Customize uploads go through it


def test_info_boxes_and_date_colors(env, tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from minibooru.web import create_app
    monkeypatch.delenv("MINIBOORU_PASSWORD", raising=False)
    with core.connect() as c:
        pid, _ = core.ingest(c, _pic(tmp_path / "w.png", "red"), ["x"], source="https://site.example/p/1", posted="2026-10-02 08:30:00")
    cl = TestClient(create_app())
    post = cl.get(f"/post/{pid}").text
    assert '<div class="note postmeta">' in post and '<span class="d-posted">2026-10-02 08:30:00 UTC</span>' in post
    assert '<span class="d-saved">' in post and 'rel="noopener noreferrer"' in post
    for key in ('["Info boxes", [["note", "Background"], ["notefg", "Text"]]]', '["dposted", "Upload date"]', '["dsaved", "Saved date"]'):
        assert key in post                                                     # colour roles, grouped by box (Customize screen)
    scrape = cl.get("/scrape").text
    assert scrape.count("▶ Start scrape now") == 1 and scrape.count("▶ Start auto rescan") >= 1
    assert scrape.index('id="oncebox"') < scrape.index("▶ Start scrape now") < scrape.index('id="autobox"') < scrape.index('id="autostart"')
    assert scrape.count('class="note"') >= 6 and '<p class="mut">' not in scrape


def test_grid_has_posts_per_row_picker_and_outline_option(env, monkeypatch):
    from fastapi.testclient import TestClient
    from minibooru.web import create_app
    monkeypatch.delenv("MINIBOORU_PASSWORD", raising=False)
    page = TestClient(create_app()).get("/").text
    for k, vals in (("colsd", ["", "4", "6", "8"]), ("colsm", ["", "1", "2", "4"])):   # desktop and phone choices
        i = page.index(f'data-k="{k}"')
        seg = page[i:page.index("</span>", page.index("Per row", i) + 20)]
        assert [v for v in vals if f'data-v="{v}"' in seg] == vals
    assert "html[data-colsd] .grid" in page and "--poutw" in page


def test_pages_hold_48_posts(env):
    with core.connect() as c:
        for i in range(49):
            c.execute("INSERT INTO posts(sha256, ext, mime) VALUES (?, 'png', 'image/png')", (f"s{i}",))
        first, total = core.search(c, "")
        second, _ = core.search(c, "", page=2)
    assert core.PER_PAGE == 48 and len(first) == 48 and len(second) == 1 and total == 49


def test_customize_has_a_colour_for_every_kind_of_box(env, monkeypatch):
    from fastapi.testclient import TestClient
    from minibooru.web import create_app
    monkeypatch.delenv("MINIBOORU_PASSWORD", raising=False)
    cl = TestClient(create_app())
    page = cl.get("/").text
    assert 'href="/customize"' in page and "Customize screen" in page          # the popup points to the page
    for group in ("Page", "Header", "Cards, panels & popups", "Info boxes", "Input boxes (text fields, dropdowns)",
                  "Main buttons", "Other buttons", "Links & dates"):
        assert '["' + group.replace("&", "\\u0026") + '", [' in page         # (embedded as safe JSON: & is escaped)
    # each new role defaults to the colour it always had, so nothing changes until you pick one
    assert ":root{--headfg:var(--fg);--notefg:var(--fg);--inbg:var(--card);--infg:var(--fg);--inline:var(--line)" in page
    assert "border:1px solid var(--inline);background:var(--inbg);color:var(--infg)" in page
    screen = cl.get("/customize")
    assert screen.status_code == 200
    for bit in ('id="setups"', 'id="cs-colors"', 'id="l-pout"', 'id="l-radius"', 'class="pv"', 'data-g="Header"', "Customize screen"):
        assert bit in screen.text

"""FastAPI web UI. Set MINIBOORU_PASSWORD to require HTTP Basic auth (user: admin)."""
import hmac
import io
import json
import html
import os
import re
import secrets
import shutil
import tempfile
import threading
import time
import uuid
import zipfile
from datetime import date
from pathlib import Path
from urllib.parse import quote, urlencode

from fastapi import FastAPI, File, Form, HTTPException, Query, Request, Response, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from PIL import Image

from . import clip, core, scraper, similar, themes, watcher

SESSION_DAYS = 30
PLACEHOLDER_PASSWORDS = ("change-me-to-something-long", "change-me")
_fails: list[float] = []     # recent failed logins (global: behind Docker every client looks like the same IP)


def _session_key() -> bytes:
    """Random secret kept in the data folder, mixed with the password so changing the password logs everyone out."""
    f = core.DATA / ".session_key"
    if not f.exists():
        f.write_bytes(secrets.token_bytes(32))
        f.chmod(0o600)
    return hmac.new(f.read_bytes(), os.environ.get("MINIBOORU_PASSWORD", "").encode(), "sha256").digest()


def make_session() -> str:
    exp = str(int(time.time()) + SESSION_DAYS * 86400)
    return exp + "." + hmac.new(_session_key(), exp.encode(), "sha256").hexdigest()


def valid_session(token: str | None) -> bool:
    try:
        exp, sig = (token or "").split(".", 1)
        good = hmac.new(_session_key(), exp.encode(), "sha256").hexdigest()
        return hmac.compare_digest(sig, good) and int(exp) > time.time()
    except ValueError:
        return False


LOGIN_PAGE = """<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover"><link rel="manifest" href="/manifest.webmanifest"><meta name="apple-mobile-web-app-capable" content="yes"><meta name="mobile-web-app-capable" content="yes"><meta name="apple-mobile-web-app-status-bar-style" content="black-translucent"><meta name="theme-color" content="#1e3a8a"><meta name="apple-mobile-web-app-title" content="{name}"><link rel="icon" href="/favicon.ico?v={iv}" sizes="any"><link rel="icon" type="image/png" href="/brand/favicon-64.png?v={iv}"><link rel="apple-touch-icon" href="/brand/apple-touch-icon.png?v={iv}">
<title>{name} login</title><style>
:root{color-scheme:light dark}body{margin:0;min-height:100vh;display:grid;place-items:center;font:16px system-ui,sans-serif;background:#f6f6f9;color:#1c1c1e}
@media(prefers-color-scheme:dark){body{background:#141416;color:#e8e8ea}}
form{width:min(340px,90vw);padding:24px;border-radius:14px;background:#8881;border:1px solid #8884;display:flex;flex-direction:column;gap:12px}
h1{margin:0;font-size:20px}input,button{font:inherit;padding:10px 12px;border-radius:8px;border:1px solid #8886}
button{background:#2f6fed;color:#fff;border:0;cursor:pointer}.err{color:#e5484d;margin:0}</style></head><body>
<form method="post" action="/login"><h1>{name}</h1>{error}
<input type="hidden" name="next" value="{next}"><input type="password" name="password" placeholder="Password" autofocus required>
<button>Log in</button></form></body></html>"""


def safe_next(url: str, default: str = "/") -> str:
    """Only allow redirects to paths on this site."""
    return url if url.startswith("/") and not url.startswith("//") else default


LETTERS = "abcdefghijklmnopqrstuvwxyz0123456789"
BG_MAX_BYTES = 20 * 1024 * 1024


def letter_groups(rows):
    """Tags -> [(section id, label, [tags])] in the order a-z, 0-9, then '#' (id 'etc') for anything else
    (symbols, other scripts). Empty sections are left out."""
    groups: dict[str, list] = {}
    for r in rows:
        first = (r["name"][:1] or "#").lower()
        groups.setdefault(first if first in LETTERS else "#", []).append(r)
    return [("etc" if k == "#" else k, k.upper(), groups[k]) for k in [*LETTERS, "#"] if k in groups]


def page_url(base: str, page: int, **params) -> str:
    params = {k: v for k, v in params.items() if v}
    if page > 1:
        params["page"] = page
    return base + ("?" + urlencode(params) if params else "")


def list_qs(q: str, sort: str, view: str, src: str = "all") -> str:
    """Query string that tells a post page which list it was opened from, so Prev/Next follow that list."""
    params = {k: v for k, v in (("q", q), ("sort", None if sort == "new" else sort), ("view", None if view == "all" else view),
                                ("src", None if src == "all" else src)) if v}
    return "?" + urlencode(params) if params else ""


def search_page(c, q: str, page: int, sort: str, view: str, per: int = core.PER_PAGE, src: str = "all"):
    """core.search with the page number clamped to 1..last page (a typed page past the end shows the last page)."""
    rows, total = core.search(c, q, max(page, 1), per=per, sort=sort, view=view, src=src)
    pages = max(1, -(-total // per))
    if page > pages or page < 1:
        page = min(max(page, 1), pages)
        rows, total = core.search(c, q, page, per=per, sort=sort, view=view, src=src)
    return rows, total, page, pages


def human_size(n: float) -> str:
    """1536 -> '1.5 KB', 3.2e9 -> '3.2 GB'."""
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024


def read_stop(mode: str, count: int, until_text: str):
    """The 'stop after N posts' or 'stop at a date' choice -> (limit, until_iso, human text). Raises 400 on a bad date."""
    if mode == "date":
        d = scraper.parse_user_date(until_text)
        if d is None:
            raise HTTPException(400, f"Could not read \"{until_text}\" as a date. Try 03/15/2024, 2024-03-15 or Mar 15, 2024.")
        if d > date.today():
            raise HTTPException(400, f"{scraper.describe_date(d)} is in the future. Pick a day in the past.")
        return watcher.SAFETY_LIMIT, d.isoformat(), f"posts dated before {scraper.describe_date(d)}"
    n = max(1, min(count, 5000))
    return n, "", f"{n} imported posts"


def watch_rows(c) -> list[dict]:
    now, out = time.time(), []
    for w in c.execute("SELECT * FROM watches ORDER BY id"):
        d = dict(w)
        d["state"] = ("running" if watcher.running_wid == w["id"] else "paused" if not w["enabled"]
                      else "queued" if w["next_run"] <= now else "waiting")
        d["in_min"] = max(1, -(-int(w["next_run"] - now) // 60))
        d["stop_text"] = (f"back to {scraper.describe_date(date.fromisoformat(w['until']))}" if w["until"]
                          else f"up to {w['max_posts']} new posts")
        out.append(d)
    return out


def create_app(scheduler: bool = False) -> FastAPI:
    core.DATA.mkdir(parents=True, exist_ok=True)
    (core.DATA / "files").mkdir(exist_ok=True)
    (core.DATA / "thumbs").mkdir(exist_ok=True)
    app = FastAPI(docs_url=None, redoc_url=None)
    password = os.environ.get("MINIBOORU_PASSWORD")
    if password and password.strip() in PLACEHOLDER_PASSWORDS:
        raise SystemExit("MINIBOORU_PASSWORD is still the example value: set your own password in .env.minibooru")
    jobs: dict[str, dict] = {}
    brand: dict = {}                      # this install's name and icon version (shown on every page)

    def refresh_brand():
        with core.connect() as c:
            brand.update(core.brand_info(c))

    refresh_brand()
    skip_setup = bool(os.environ.get("MINIBOORU_SKIP_SETUP"))
    if scheduler:
        watcher.start()
        threading.Thread(target=similar.backfill, args=(core.connect, core.DATA, print), daemon=True).start()

    @app.middleware("http")
    async def first_run_setup(request: Request, call_next):
        # registered before login_required, so it runs after it: only logged-in visitors get here.
        # A new install asks for its name (and icon) once, before showing anything else.
        p = request.url.path
        if (not brand.get("configured") and not skip_setup and request.method == "GET" and p != "/setup"
                and not p.startswith(("/static/", "/brand/", "/files/", "/thumbs/", "/background/")) and p not in ("/login", "/favicon.ico", "/manifest.webmanifest")):
            dest = p + ("?" + request.url.query if request.url.query else "")
            return RedirectResponse("/setup?" + urlencode({"next": dest}), 303)
        return await call_next(request)

    @app.middleware("http")
    async def login_required(request: Request, call_next):
        # every page, file and thumbnail needs the session cookie that /login hands out
        # the app icon is public so the login page and the browser tab can show it
        public = (request.url.path in ("/login", "/favicon.ico", "/manifest.webmanifest")   # phones read the manifest before login
                  or request.url.path.startswith(("/static/", "/brand/")))
        if password and not public and not valid_session(request.cookies.get("mb_session")):
            if request.method == "GET":
                dest = request.url.path + ("?" + request.url.query if request.url.query else "")
                return RedirectResponse("/login?" + urlencode({"next": dest}), 303)
            return PlainTextResponse("login required", status_code=401)
        return await call_next(request)

    @app.middleware("http")
    async def same_origin_only(request: Request, call_next):
        # minimal CSRF guard: browsers send Origin on cross-site POSTs
        if request.method == "POST":
            origin = request.headers.get("origin")
            if origin and origin.split("://", 1)[-1] != request.headers.get("host"):
                return PlainTextResponse("cross-origin POST refused", status_code=403)
        return await call_next(request)

    @app.middleware("http")
    async def safety_headers(request: Request, call_next):
        resp = await call_next(request)
        resp.headers.setdefault("X-Content-Type-Options", "nosniff")   # a file is only ever what its type says
        resp.headers.setdefault("X-Frame-Options", "DENY")             # no other site can show the app in a frame
        resp.headers.setdefault("Referrer-Policy", "same-origin")       # links out don't reveal the app's address
        if request.url.path.startswith(("/files/", "/thumbs/", "/background/", "/brand/")):
            resp.headers["Content-Security-Policy"] = "sandbox"          # stored media can never run script
        return resp
    tpl = Jinja2Templates(directory=Path(__file__).parent / "templates")
    tpl.env.globals["auth_on"] = bool(password)
    tpl.env.filters["duration"] = scraper.fmt_duration
    tpl.env.globals["bg_versions"] = core.background_versions
    tpl.env.globals["brand"] = brand
    tpl.env.globals["theme_cfg"] = themes.config()

    @app.exception_handler(HTTPException)
    async def friendly_errors(request: Request, exc: HTTPException):
        if request.method == "GET" and exc.status_code == 404 and request.url.path.startswith(("/files/", "/thumbs/")):
            return PlainTextResponse("not found", status_code=404)
        resp = tpl.TemplateResponse(request, "error.html", {"message": exc.detail}, status_code=exc.status_code)
        for k, v in (exc.headers or {}).items():
            resp.headers[k] = v
        return resp

    def login_page(nxt: str, error: str = "", status: int = 200):
        err = f'<p class="err">{html.escape(error)}</p>' if error else ""
        page = LOGIN_PAGE.replace("{name}", html.escape(brand["name"])).replace("{iv}", str(brand["icon_v"]))
        return HTMLResponse(page.replace("{error}", err).replace("{next}", html.escape(safe_next(nxt), quote=True)), status)

    @app.get("/login")
    def login_form(next: str = "/"):
        return login_page(next)

    @app.post("/login")
    def login(password_in: str = Form("", alias="password"), next: str = Form("/")):
        now = time.time()
        _fails[:] = [t for t in _fails if now - t < 300]
        if len(_fails) >= 5:
            return login_page(next, "Too many wrong attempts. Wait a few minutes.", 429)
        if not password or not secrets.compare_digest(password_in.encode(), password.encode()):
            _fails.append(now)
            time.sleep(1)
            return login_page(next, "Wrong password.", 401)
        resp = RedirectResponse(safe_next(next), 303)
        resp.set_cookie("mb_session", make_session(), max_age=SESSION_DAYS * 86400, httponly=True, samesite="lax")
        return resp

    @app.post("/logout")
    def logout():
        resp = RedirectResponse("/login", 303)
        resp.delete_cookie("mb_session")
        return resp

    # static mounts sit behind the same app-level auth dependency
    app.mount("/files", StaticFiles(directory=core.DATA / "files"), name="files")
    app.mount("/thumbs", StaticFiles(directory=core.DATA / "thumbs"), name="thumbs")
    app.mount("/static", StaticFiles(directory=Path(__file__).parent / "static"), name="static")   # app icons

    def icon_file(name: str) -> Path:
        custom = core.branding_dir() / name
        return custom if custom.exists() else Path(__file__).parent / "static" / name

    @app.get("/manifest.webmanifest", include_in_schema=False)
    def manifest():
        """Lets phones install the app on the home screen (full screen, this install's name and icon)."""
        v = brand["icon_v"]
        return JSONResponse({
            "name": brand["name"], "short_name": brand["name"][:12], "start_url": "/", "scope": "/", "id": "/",
            "display": "standalone", "background_color": "#0a1224", "theme_color": "#1e3a8a",
            "icons": [{"src": f"/brand/icon-192.png?v={v}", "sizes": "192x192", "type": "image/png", "purpose": "any"},
                      {"src": f"/brand/apple-touch-icon.png?v={v}", "sizes": "180x180", "type": "image/png"}]},
            media_type="application/manifest+json")

    @app.get("/favicon.ico", include_in_schema=False)
    def favicon():
        return FileResponse(icon_file("favicon.ico"), media_type="image/x-icon")

    @app.get("/brand/{name}", include_in_schema=False)
    def brand_icon(name: str):
        if name not in core.ICON_FILES:
            raise HTTPException(404)
        return FileResponse(icon_file(name), media_type="image/x-icon" if name.endswith(".ico") else "image/png")

    # ---------- save a post from a site you are logged in to (bookmarklet + popup) ----------
    @app.get("/clip/bookmarklet")
    def clip_bookmarklet(request: Request):
        return tpl.TemplateResponse(request, "clip_bookmarklet.html", {})

    @app.get("/clip/extension.zip")
    def clip_extension(request: Request, app_url: str = Query("", alias="app")):
        """The "Save to ..." toolbar button for Brave/Chrome/Edge: runs the same code as the bookmark, with this
        install's name and icon. It may only touch the tab it is clicked on (activeTab), nothing else."""
        origin = app_url.rstrip("/") or f"{request.url.scheme}://{request.url.netloc}"
        if not re.fullmatch(r"https?://[A-Za-z0-9.\-\[\]:]+", origin):
            raise HTTPException(400, "app must be this app's address, like http://host:8090")
        name = brand["name"]
        runner = (Path(__file__).parent / "templates" / "_clip_runner.js").read_text()
        manifest = {"manifest_version": 3, "name": f"Save to {name}", "version": "1.0",
                    "description": f"Saves the post you are looking at to {name} ({origin}), with your own login.",
                    "permissions": ["activeTab", "scripting"],
                    "icons": {str(n): f"icon-{n}.png" for n in (16, 32, 48, 128)},
                    "action": {"default_title": f"Save to {name}", "default_icon": {str(n): f"icon-{n}.png" for n in (16, 32, 48)}},
                    "background": {"service_worker": "background.js"}}
        background = (runner + "\nconst APP = " + json.dumps(origin) + ";\n"
                      "chrome.action.onClicked.addListener(tab => {\n"
                      "  if (!tab.id || !/^https?:/.test(tab.url || '')) return;   /* not on browser pages */\n"
                      "  chrome.scripting.executeScript({target: {tabId: tab.id}, world: 'MAIN', func: mbClipRun, args: [APP]});\n"
                      "});\n")
        buf = io.BytesIO()
        with Image.open(icon_file("icon-192.png")) as im, zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
            im = im.convert("RGBA")
            for n in (16, 32, 48, 128):
                out = io.BytesIO()
                im.resize((n, n), Image.LANCZOS).save(out, "PNG")
                z.writestr(f"save-to-app/icon-{n}.png", out.getvalue())
            z.writestr("save-to-app/manifest.json", json.dumps(manifest, indent=2, ensure_ascii=False))
            z.writestr("save-to-app/background.js", background)
        fname = re.sub(r"[^A-Za-z0-9]+", "-", f"save-to-{name}").strip("-").lower() or "save-to-app"
        return Response(buf.getvalue(), media_type="application/zip",
                        headers={"Content-Disposition": f'attachment; filename="{fname}.zip"'})

    @app.get("/clip")
    def clip_page(request: Request):
        with core.connect() as c:
            types = core.tag_types(c)
        return tpl.TemplateResponse(request, "clip.html", {"types": types})

    @app.post("/clip/parse")
    async def clip_parse(request: Request):
        body = await request.body()
        if len(body) > clip.MAX_HTML + 4096:
            raise HTTPException(413, "page too large")
        try:
            data = json.loads(body)
            with core.connect() as c:
                return clip.parse(c, str(data.get("html", "")), str(data.get("url", "")))
        except (ValueError, AttributeError) as e:
            raise HTTPException(400, f"could not read that page: {e}")

    @app.post("/clip/save")
    async def clip_save(request: Request):
        form = await request.form(max_files=200, max_part_size=4 * 1024 * 1024)
        try:
            meta = json.loads(str(form.get("meta", "")))
            url = str(meta.get("url", ""))
            with core.connect() as c:
                tags = clip.clean_tags(meta.get("tags"), core.type_names(c))
            missing = [u for u in meta.get("missing", []) if isinstance(u, str) and u.startswith(("http://", "https://"))][:100]
        except (ValueError, AttributeError) as e:
            raise HTTPException(400, f"bad request: {e}")
        cap = int(os.environ.get("MINIBOORU_MAX_MB", 300)) * 1024 * 1024
        with tempfile.TemporaryDirectory() as tmp:
            files = []
            for i, part in enumerate(v for k, v in form.multi_items() if k == "files" and hasattr(v, "read")):
                d = Path(tmp) / f"b{i}"
                d.mkdir()
                raw = await part.read(cap + 1)
                if len(raw) > cap:
                    raise HTTPException(413, "a file is larger than the size limit")
                ext = Path(part.filename or "").suffix.lower()
                if ext not in scraper.MEDIA_EXT:
                    ext = {"image/jpeg": ".jpg", "image/png": ".png", "image/gif": ".gif", "image/webp": ".webp",
                           "video/mp4": ".mp4", "video/webm": ".webm", "video/quicktime": ".mov"}.get(
                               (part.content_type or "").split(";")[0].strip().lower(), "")
                if not ext:
                    continue   # not a picture or video
                (d / f"file{ext}").write_bytes(raw)
                files.append(d / f"file{ext}")
            try:
                with core.connect() as c:
                    return clip.save(c, url, str(meta.get("title", ""))[:300], meta.get("posted"), tags, files, Path(tmp), missing)
            except ValueError as e:
                raise HTTPException(400, str(e))

    @app.get("/customize")
    def customize_screen(request: Request):
        with core.connect() as c:
            sample, _ = core.search(c, "", per=3)       # a few of your own posts for the preview
        return tpl.TemplateResponse(request, "customize.html", {"sample": sample})

    @app.get("/setup")
    def setup_form(request: Request, next: str = "/"):
        return tpl.TemplateResponse(request, "setup.html", {"next": safe_next(next), "default_name": core.DEFAULT_NAME})

    @app.post("/setup")
    async def setup_save(name: str = Form(""), next: str = Form("/"), icon: UploadFile | None = File(None),
                         types: str = Form("")):
        try:
            clean = core.clean_site_name(name or core.DEFAULT_NAME)
            type_list = [t for t in (x.strip() for x in types.replace("\n", ",").split(",")) if t]
            type_list = list(dict.fromkeys(core.clean_type_name(t) for t in type_list if t.lower() != core.OTHER))
            raw = await icon.read(BG_MAX_BYTES + 1) if icon is not None and icon.filename else b""
            if len(raw) > BG_MAX_BYTES:
                raise ValueError("picture too large (20 MB max)")
            if raw:
                core.set_icon(raw)
        except ValueError as e:
            raise HTTPException(400, str(e))
        with core.connect() as c:
            core.set_setting(c, "site_name", clean)
            core.set_tag_types(c, type_list)
            if core.get_setting(c, "bg_dim") is None:
                core.set_setting(c, "bg_dim", core.NEW_INSTALL_DIM)
        refresh_brand()
        return RedirectResponse(safe_next(next), 303)

    @app.post("/settings/name")
    def settings_name(name: str = Form("")):
        try:
            clean = core.clean_site_name(name)
        except ValueError as e:
            raise HTTPException(400, str(e))
        with core.connect() as c:
            core.set_setting(c, "site_name", clean)
        refresh_brand()
        return {"ok": True, "name": clean}

    @app.post("/settings/icon")
    async def settings_icon(file: UploadFile = File(...)):
        raw = await file.read(BG_MAX_BYTES + 1)
        if len(raw) > BG_MAX_BYTES:
            raise HTTPException(413, "picture too large (20 MB max)")
        try:
            core.set_icon(raw)
        except ValueError as e:
            raise HTTPException(400, str(e))
        refresh_brand()
        return {"ok": True, "icon_v": brand["icon_v"]}

    @app.post("/settings/icon/reset")
    def settings_icon_reset():
        core.reset_icon()
        refresh_brand()
        return {"ok": True, "icon_v": brand["icon_v"]}

    @app.get("/")
    def index(request: Request, q: str = "", page: int = 1, sort: str = "new", view: str = "all", src: str = "all"):
        sort = sort if sort in core.SORTS else "new"
        view = view if view in core.VIEWS else "all"
        src = src if src in core.SOURCES else "all"
        keep = dict(q=q, sort=None if sort == "new" else sort, view=None if view == "all" else view, src=None if src == "all" else src)
        with core.connect() as c:
            rows, total, page, pages = search_page(c, q, page, sort, view, src=src)
            ptags = core.tags_for_posts(c, [r["id"] for r in rows])
            twords = core.title_words(c, q) if q else []
        return tpl.TemplateResponse(request, "index.html", {
            "posts": rows, "ptags": ptags, "total": total, "q": q, "title_words": twords, "page": page, "pages": pages, "sort": sort, "view": view, "views": core.VIEWS,
            "src": src, "sources": core.SOURCES, "here": page_url("/", page, **keep),
            "post_qs": list_qs(q, sort, view, src), "jump_action": "/",
            "prev_url": page_url("/", page - 1, **keep) if page > 1 else None,
            "next_url": page_url("/", page + 1, **keep) if page < pages else None})

    @app.get("/post/{pid}")
    def post(request: Request, pid: int, q: str = "", sort: str = "new", view: str = "all", src: str = "all"):
        sort = sort if sort in core.SORTS else "new"
        view = view if view in core.VIEWS else "all"
        src = src if src in core.SOURCES else "all"
        with core.connect() as c:
            prev_id, next_id = core.neighbors(c, pid, q, sort, view, src)
            p = c.execute("SELECT * FROM posts WHERE id=?", (pid,)).fetchone()
            if not p:
                raise HTTPException(404)
            tags = core.post_tags(c, pid)
            files = core.post_files(c, pid)
            dups = c.execute("""SELECT o.*, d.dist FROM dup_flags d JOIN posts o ON o.id = CASE WHEN d.a=? THEN d.b ELSE d.a END
                WHERE (d.a=? OR d.b=?) AND d.status='open' ORDER BY d.dist""", (pid, pid, pid)).fetchall()
        qs = list_qs(q, sort, view, src)
        return tpl.TemplateResponse(request, "post.html", {"p": p, "tags": tags, "files": files, "dups": dups,
            "prev_url": f"/post/{prev_id}{qs}" if prev_id else None, "next_url": f"/post/{next_id}{qs}" if next_id else None,
            "list_url": "/" + qs})

    @app.post("/post/{pid}/tags")
    def edit_tags(pid: int, tags: str = Form(""), title: str = Form("")):
        with core.connect() as c:
            core.set_tags(c, pid, tags.split())
            c.execute("UPDATE posts SET title=? WHERE id=?", (title, pid))
        return RedirectResponse(f"/post/{pid}", 303)

    @app.post("/post/{pid}/favorite")
    def toggle_favorite(pid: int):
        with core.connect() as c:
            c.execute("UPDATE posts SET favorite=1-favorite WHERE id=?", (pid,))
        return RedirectResponse(f"/post/{pid}", 303)

    @app.post("/post/{pid}/file/{sha}/delete")
    def delete_one_file(pid: int, sha: str):
        with core.connect() as c:
            try:
                core.remove_file(c, pid, sha)
            except ValueError as e:
                raise HTTPException(400, str(e))
        return RedirectResponse(f"/post/{pid}", 303)

    @app.get("/background/{slot}")
    def background(slot: str):
        try:
            p = core.background_path(slot)
        except ValueError:
            raise HTTPException(404)
        if not p.exists():
            raise HTTPException(404)
        return FileResponse(p, media_type="image/jpeg", headers={"Cache-Control": "private, max-age=31536000"})  # URL carries ?v=

    @app.post("/background/{slot}")
    async def background_set(slot: str, file: UploadFile = File(...)):
        raw = await file.read(BG_MAX_BYTES + 1)
        if len(raw) > BG_MAX_BYTES:
            raise HTTPException(413, "picture too large (20 MB max)")
        try:
            core.set_background(slot, raw)
        except ValueError as e:
            raise HTTPException(400, str(e))
        return {"ok": True, "versions": core.background_versions()}

    @app.post("/background/{slot}/clear")
    def background_clear(slot: str):
        try:
            core.background_path(slot).unlink(missing_ok=True)
        except ValueError:
            raise HTTPException(404)
        return {"ok": True, "versions": core.background_versions()}

    @app.post("/post/{pid}/delete")
    def delete(pid: int, next: str = Form("/")):
        with core.connect() as c:
            core.delete_post(c, pid)
        return RedirectResponse(safe_next(next), 303)

    @app.post("/post/{pid}/dup/{other}/{action}")
    def dup_action(pid: int, other: int, action: str):
        a, b = sorted((pid, other))
        with core.connect() as c:
            if action == "dismiss":      # not a duplicate: keep both, stop flagging
                c.execute("UPDATE dup_flags SET status='dismissed' WHERE a=? AND b=?", (a, b))
            elif action == "merge":      # pid is the copy: its tags and favorite go to `other`, then pid is deleted
                if c.execute("SELECT COUNT(*) FROM posts WHERE id IN (?,?)", (pid, other)).fetchone()[0] == 2:
                    core.add_tags(c, other, [r["name"] for r in core.post_tags(c, pid)])
                    c.execute("UPDATE posts SET favorite=MAX(favorite, (SELECT favorite FROM posts WHERE id=?)) WHERE id=?", (pid, other))
                    core.delete_post(c, pid)
                    return RedirectResponse(f"/post/{other}", 303)
            else:
                raise HTTPException(404)
        return RedirectResponse(f"/post/{pid}", 303)

    disk_cache: dict = {}

    def data_size() -> dict:
        """Bytes used by pictures/videos and thumbnails (walking 30,000+ files takes a moment: kept for 10 minutes)."""
        if disk_cache.get("at", 0) > time.time() - 600:
            return disk_cache
        out = {}
        for part in ("files", "thumbs"):
            total, stack = 0, [core.DATA / part]
            while stack:
                try:
                    with os.scandir(stack.pop()) as it:
                        for e in it:
                            if e.is_dir(follow_symlinks=False):
                                stack.append(e.path)
                            elif e.is_file(follow_symlinks=False):
                                total += e.stat(follow_symlinks=False).st_size
                except OSError:
                    pass
            out[part] = total
        db = core.DATA / "db.sqlite"
        out["db"] = db.stat().st_size if db.exists() else 0
        du = shutil.disk_usage(core.DATA)
        out.update(free=du.free, disk=du.total, at=time.time())
        disk_cache.clear()
        disk_cache.update(out)
        return out

    @app.get("/stats")
    def stats(request: Request):
        with core.connect() as c:
            one = lambda sql, *a: c.execute(sql, a).fetchone()[0]
            files = """SELECT mime FROM posts UNION ALL SELECT mime FROM post_media"""
            nums = {"posts": one("SELECT COUNT(*) FROM posts"),
                    "files": one(f"SELECT COUNT(*) FROM ({files})"),
                    "videos": one(f"SELECT COUNT(*) FROM ({files}) WHERE mime LIKE 'video/%'"),
                    "tags": one("SELECT COUNT(DISTINCT tag_id) FROM post_tags"),
                    "fav": one("SELECT COUNT(*) FROM posts WHERE favorite=1"),
                    "dups": one("SELECT COUNT(*) FROM dup_flags WHERE status='open'"),
                    "bookmarked": one(f"SELECT COUNT(*) FROM posts p WHERE {core.BOOKMARK_SQL}"),
                    "week": one("SELECT COUNT(*) FROM posts WHERE created >= datetime('now', '-7 days')"),
                    "runs": one("SELECT COUNT(*) FROM scrape_runs"),
                    "imported": one("SELECT COALESCE(SUM(new), 0) FROM scrape_runs")}
            nums["pictures"] = nums["files"] - nums["videos"]
            months = c.execute("""SELECT substr(COALESCE(posted, created), 1, 7) AS m, COUNT(*) AS n FROM posts
                                  GROUP BY m ORDER BY m DESC LIMIT 24""").fetchall()[::-1]
            days = c.execute("""SELECT substr(created, 1, 10) AS d, COUNT(*) AS n FROM posts
                                WHERE created >= date('now', '-13 days') GROUP BY d ORDER BY d""").fetchall()
            top = c.execute("""SELECT t.name, cat.color, COUNT(*) AS n FROM post_tags pt JOIN tags t ON t.id=pt.tag_id
                               JOIN categories cat ON cat.name=t.category WHERE t.name != ?
                               GROUP BY pt.tag_id ORDER BY n DESC LIMIT 24""", (core.UPLOAD_TAG,)).fetchall()
            last = c.execute("SELECT * FROM scrape_runs ORDER BY id DESC LIMIT 1").fetchone()
        disk = data_size()
        per_post = (disk["files"] / nums["posts"]) if nums["posts"] else 0
        return tpl.TemplateResponse(request, "stats.html", {
            "n": nums, "months": months, "mmax": max([r["n"] for r in months] or [1]), "days": days,
            "dmax": max([r["n"] for r in days] or [1]), "top": top, "last": last, "disk": disk,
            "posts_left": int(disk["free"] / per_post) if per_post else None, "size": human_size})

    @app.get("/tags/suggest")
    def tags_suggest(q: str = ""):
        """Tags for the search box as you type: names starting with what you typed first, then ones containing it,
        most-used first."""
        word = core.normalize_tag(q.lstrip("-"))[:40]
        if not word:
            return []
        like = word.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        sql = """SELECT t.name, cat.color, (SELECT COUNT(*) FROM post_tags pt WHERE pt.tag_id=t.id) AS n
                 FROM tags t JOIN categories cat ON cat.name=t.category WHERE t.name LIKE ? ESCAPE '\\'
                 ORDER BY n DESC, t.name LIMIT 8"""
        with core.connect() as c:
            rows = c.execute(sql, (like + "%",)).fetchall()
            if len(rows) < 8:
                seen = {r["name"] for r in rows}
                rows += [r for r in c.execute(sql, ("%" + like + "%",)).fetchall() if r["name"] not in seen][:8 - len(rows)]
        return [{"name": r["name"], "color": r["color"], "n": r["n"]} for r in rows if r["n"]]

    @app.get("/dups/review")
    def dups_review(request: Request, skip: int = 0):
        """Possible duplicates one pair at a time, side by side, with one-key choices."""
        skip = max(0, skip)
        with core.connect() as c:
            total = c.execute("SELECT COUNT(*) FROM dup_flags WHERE status='open'").fetchone()[0]
            skip = min(skip, max(0, total - 1))
            row = c.execute("SELECT a, b, dist FROM dup_flags WHERE status='open' ORDER BY dist, a, b LIMIT 1 OFFSET ?",
                            (skip,)).fetchone()
            sides = []
            if row:
                for pid in (row["a"], row["b"]):
                    p = c.execute("SELECT * FROM posts WHERE id=?", (pid,)).fetchone()
                    files = core.post_files(c, pid)
                    host = re.sub(r"^https?://(www\.)?", "", p["source"] or "").split("/")[0]
                    sides.append({"p": p, "files": files, "tags": core.post_tags(c, pid), "host": host,
                                  "pixels": (p["width"] or 0) * (p["height"] or 0)})
        hint = None
        if sides:   # a nudge only: the bigger picture, then the one with more files, then more tags
            score = [(s["pixels"], len(s["files"]), len(s["tags"])) for s in sides]
            if score[0] != score[1]:
                hint = 0 if score[0] > score[1] else 1
        return tpl.TemplateResponse(request, "dups.html", {"sides": sides, "dist": row["dist"] if row else None,
                                                           "total": total, "skip": skip, "hint": hint})

    @app.post("/posts/bulk-delete")
    def post_bulk_delete(ids: list[int] = Form([]), next: str = Form("/")):
        with core.connect() as c:
            for pid in ids:
                core.delete_post(c, pid)
        return RedirectResponse(safe_next(next), 303)

    @app.post("/upload")
    async def upload(files: list[UploadFile] = File(...), tags: str = Form(""), title: str = Form(""),
                     together: str = Form("")):
        """One post per file, or (together) one post holding all the files in the order picked."""
        with core.connect() as c, tempfile.TemporaryDirectory() as tmp:
            pid = None
            for i, f in enumerate(files):
                dest = Path(tmp) / str(i) / Path(f.filename or "upload").name
                dest.parent.mkdir()
                dest.write_bytes(await f.read())
                try:
                    if together and pid is not None:
                        core.add_media(c, pid, dest)
                    else:
                        pid, _ = core.ingest(c, dest, tags.split() + [core.UPLOAD_TAG], title=title or dest.stem, source="upload")
                except ValueError as e:
                    raise HTTPException(400, str(e))
        return RedirectResponse(f"/post/{pid}" if together and pid else "/", 303)

    @app.get("/tags")
    def tag_list(request: Request):
        with core.connect() as c:
            rows = c.execute("""SELECT t.id, t.name, t.category, cat.color, COUNT(pt.post_id) n
                FROM tags t JOIN categories cat ON cat.name=t.category
                LEFT JOIN post_tags pt ON pt.tag_id=t.id GROUP BY t.id ORDER BY t.name COLLATE NOCASE""").fetchall()
            aliases = c.execute("SELECT a.alias, t.name FROM aliases a JOIN tags t ON t.id=a.tag_id").fetchall()
            imps = c.execute("""SELECT a.name, b.name FROM implications i
                JOIN tags a ON a.id=i.tag_id JOIN tags b ON b.id=i.implies_id""").fetchall()
            blocked = [r[0] for r in c.execute("SELECT name FROM blocked_tags ORDER BY name")]
            types = core.tag_types(c)
            rule = core.tag_rule(c)
        sections = [(k, color, letter_groups([r for r in rows if r["category"] == k])) for k, color in types]
        return tpl.TemplateResponse(request, "tags.html", {
            "sections": sections, "cats": [k for k, _ in types], "types": types, "rule": rule,
            "other": core.OTHER, "blocked": blocked, "aliases": aliases, "imps": imps})

    def _type_action(fn):
        with core.connect() as c:
            try:
                fn(c)
            except ValueError as e:
                raise HTTPException(400, str(e))
        return RedirectResponse("/tags#types", 303)

    @app.post("/tags/types/add")
    def tag_type_add(name: str = Form(""), color: str = Form("")):
        return _type_action(lambda c: core.add_type(c, name, color or None))

    @app.post("/tags/types/edit")
    def tag_type_edit(name: str = Form(...), new_name: str = Form(""), color: str = Form("")):
        return _type_action(lambda c: core.edit_type(c, name, new_name or None, color or None))

    @app.post("/tags/types/delete")
    def tag_type_delete(name: str = Form(...)):
        return _type_action(lambda c: core.delete_type(c, name))

    @app.post("/tags/rule")
    def tag_rule_set(first: str = Form(core.OTHER), middle: str = Form(core.OTHER), last: str = Form(core.OTHER)):
        return _type_action(lambda c: core.set_tag_rule(c, first, middle, last))

    @app.post("/tags/bulk-category")
    def tag_bulk_category(names: list[str] = Form([]), category: str = Form(...)):
        cat = category.strip().lower()
        with core.connect() as c:
            if cat not in core.type_names(c):
                raise HTTPException(400, "no such tag type")
            for n in names:
                tid = core.lookup_tag(c, n)
                if tid is not None:
                    c.execute("UPDATE tags SET category=? WHERE id=?", (cat, tid))
        return RedirectResponse("/tags", 303)

    @app.post("/tags/category")
    def set_category(name: str = Form(...), category: str = Form(...), next: str = Form("")):
        with core.connect() as c:
            tid = core.lookup_tag(c, name)
            if category not in core.type_names(c):
                raise HTTPException(400, "no such tag type")
            if tid is not None:
                c.execute("UPDATE tags SET category=? WHERE id=?", (category, tid))
                name = c.execute("SELECT name FROM tags WHERE id=?", (tid,)).fetchone()[0]
                return RedirectResponse(safe_next(next, "/tag/" + quote(name)) if next else "/tag/" + quote(name), 303)
        return RedirectResponse("/tags", 303)

    @app.get("/tag/{name}")
    def tag_page(request: Request, name: str, page: int = 1, sort: str = "new", view: str = "all", src: str = "all"):
        sort = sort if sort in core.SORTS else "new"
        view = view if view in core.VIEWS else "all"
        src = src if src in core.SOURCES else "all"
        vp, srcp = (None if view == "all" else view), (None if src == "all" else src)
        with core.connect() as c:
            tid = core.lookup_tag(c, name)
            if tid is None:
                raise HTTPException(404, "no such tag")
            t = c.execute("SELECT t.*, cat.color FROM tags t JOIN categories cat ON cat.name=t.category WHERE t.id=?", (tid,)).fetchone()
            rows, total, page, pages = search_page(c, t["name"], page, sort, view, src=src)
            ptags = core.tags_for_posts(c, [r["id"] for r in rows])
            cats = core.type_names(c)
            aliases = [r[0] for r in c.execute("SELECT alias FROM aliases WHERE tag_id=?", (tid,))]
            implies = [r[0] for r in c.execute("SELECT b.name FROM implications i JOIN tags b ON b.id=i.implies_id WHERE i.tag_id=?", (tid,))]
        base, sp = "/tag/" + quote(t["name"]), (None if sort == "new" else sort)
        return tpl.TemplateResponse(request, "tag.html", {
            "t": t, "posts": rows, "ptags": ptags, "total": total, "page": page, "pages": pages, "cats": cats, "aliases": aliases,
            "implies": implies, "sort": sort, "view": view, "views": core.VIEWS, "src": src, "sources": core.SOURCES,
            "here": page_url(base, page, sort=sp, view=vp, src=srcp),
            "post_qs": list_qs(t["name"], sort, view, src), "jump_action": base,
            "prev_url": page_url(base, page - 1, sort=sp, view=vp, src=srcp) if page > 1 else None,
            "next_url": page_url(base, page + 1, sort=sp, view=vp, src=srcp) if page < pages else None})

    @app.post("/tag/{name}/delete")
    def tag_delete(name: str, block: str = Form(""), next: str = Form("")):
        with core.connect() as c:
            core.delete_tag(c, name, block=bool(block))
        return RedirectResponse(safe_next(next, "/tags") if next else "/tags", 303)

    @app.post("/tags/bulk-delete")
    def tag_bulk_delete(names: list[str] = Form([]), block: str = Form("")):
        with core.connect() as c:
            for n in names:
                core.delete_tag(c, n, block=bool(block))
        return RedirectResponse("/tags", 303)

    @app.post("/tags/unblock")
    def tag_unblock(name: str = Form(...)):
        with core.connect() as c:
            c.execute("DELETE FROM blocked_tags WHERE name=?", (name,))
        return RedirectResponse("/tags", 303)

    @app.post("/tags/alias")
    def alias(alias: str = Form(...), target: str = Form(...)):
        with core.connect() as c:
            try:
                core.add_alias(c, alias, target)
            except ValueError as e:
                raise HTTPException(400, str(e))
        return RedirectResponse("/tags", 303)

    @app.post("/tags/implication")
    def implication(tag: str = Form(...), implies: str = Form(...)):
        with core.connect() as c:
            try:
                core.add_implication(c, tag, implies)
            except ValueError as e:
                raise HTTPException(400, str(e))
        return RedirectResponse("/tags", 303)

    def pending_resume(c):
        """Listing page of a run that a restart cut off and that will continue by itself (None if there is none)."""
        if watcher.current:
            return None
        try:
            return json.loads(core.get_setting(c, "unfinished_run") or "null")["page"]
        except (TypeError, KeyError, ValueError):
            return None

    def scrape_context(c):
        wid, jid = watcher.running_wid, watcher.current_jid
        cur_url, cur_stop = ((f"/watch/{wid}", f"/watches/{wid}/stop") if wid is not None
                             else (f"/scrape/{jid}", f"/scrape/{jid}/stop") if jid else (None, None))
        return {"runs": c.execute("SELECT * FROM scrape_runs ORDER BY id DESC LIMIT 100").fetchall(), "watches": watch_rows(c),
                "scheduler": scheduler, "current": watcher.current, "running_wid": wid,
                "current_url": cur_url, "current_stop": cur_stop,
                "current_for": scraper.fmt_duration(time.time() - watcher.started_at) if watcher.started_at else "",
                "resume_page": pending_resume(c)}

    @app.get("/scrape")
    def scrape_form(request: Request):
        with core.connect() as c:
            ctx = scrape_context(c)
        return tpl.TemplateResponse(request, "scrape.html", {"job": None, **ctx})

    @app.get("/scrape/watches")
    def scrape_watches_fragment(request: Request):
        with core.connect() as c:
            ctx = scrape_context(c)
        return tpl.TemplateResponse(request, "_watches.html", ctx)

    RESUME_KEY = "unfinished_run"     # a manual run that was going when the app stopped (restart, deploy, power cut)
    RESUME_TRIES = 3                  # give up after this many restarts without getting past the same page

    def start_manual(spec: dict, resumed: dict | None = None) -> str:
        """Start a manual scrape from its settings (the Scrape form, or a saved unfinished run). Returns the job id.
        While it runs, its settings and current listing page are saved, so a restart can continue it."""
        if not watcher.scrape_lock.acquire(blocking=False):
            raise HTTPException(409, f"Busy: {watcher.current or 'a scrape'} is still running. Try again when it finishes (see the Scrape page).")
        url, single = spec["url"], spec["single"]
        state = dict(resumed or {"spec": spec, "started": time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime()), "resumes": 0})
        state["page"] = spec["start_page"]
        stop_text = spec["stop_text"] + (f" · starting at listing page {spec['start_page']}" if spec["start_page"] > 1 else "")
        stop_event = threading.Event()
        opt = scraper.Options(keywords=spec["keywords"], limit=spec["limit"], card=spec["card"], title=spec["title"],
                              tag=spec["tag"], media=spec["media"], date=spec["date"], until=spec["until"],
                              cancel=stop_event, single=single, start_page=spec["start_page"])
        jid = uuid.uuid4().hex[:8]
        job = jobs[jid] = {"url": url, "keywords": spec["keywords"] or ["(single post: no keyword filter)"],
                           "log": scraper.RunLog(watcher.MAX_LOG_LINES), "running": True, "stats": None,
                           "heading": "Scrape (resumed after a restart)" if resumed else "Scrape",
                           "stop_text": stop_text, "stop_url": f"/scrape/{jid}/stop", "cancel": stop_event}

        def save_state(value):
            with core.connect() as c:
                if value is None:
                    c.execute("DELETE FROM settings WHERE key=?", (RESUME_KEY,))
                else:
                    core.set_setting(c, RESUME_KEY, json.dumps(value))

        def log(line):
            job["log"].append(line)
            if not single and job["log"].page and job["log"].page != state["page"]:
                state["page"] = job["log"].page
                try:
                    save_state(state)
                except Exception:
                    pass                  # a busy database only delays the bookmark of the page

        def work():
            watcher.current, watcher.current_jid = f"manual scrape of {url}", jid
            watcher.started_at = job["log"].started
            try:
                if not single:
                    save_state(state)
                if resumed:
                    log(f"Resumed after a restart, from page {spec['start_page']}")
                with core.connect() as c:
                    run = scraper.scrape_single if single else scraper.scrape
                    job["stats"] = run(c, url, opt, log=log)
            except Exception as e:  # surface any failure in the page instead of dying silently
                job["log"].append(f"Failed: {e}")
            finally:
                # the run ended on its own (or was stopped): nothing to continue. A crash or restart never gets here.
                try:
                    save_state(None)
                except Exception:
                    pass
                job["log"].finished = time.time()
                job["running"] = False
                watcher.current = watcher.current_jid = watcher.started_at = None
                watcher.scrape_lock.release()

        threading.Thread(target=work, daemon=True).start()
        return jid

    def resume_unfinished(delay: float = 60.0):   # gives the network time to come back after a power cut
        """At start-up: continue a manual run that the last shutdown cut off, from the listing page it had reached."""
        time.sleep(delay)
        with core.connect() as c:
            raw = core.get_setting(c, RESUME_KEY)
            if not raw:
                return
            try:
                state = json.loads(raw)
                spec = dict(state["spec"])
            except (ValueError, KeyError, TypeError):
                c.execute("DELETE FROM settings WHERE key=?", (RESUME_KEY,))
                return
            page = max(1, int(state.get("page") or spec["start_page"]))
            tries = state.get("resumes", 0) + 1 if page == state.get("last_resume_page") else 1
            if tries > RESUME_TRIES:
                c.execute("DELETE FROM settings WHERE key=?", (RESUME_KEY,))
                print(f"Not resuming the unfinished scrape: it stopped at page {page} {RESUME_TRIES} times in a row.")
                return
            if not spec["until"]:      # 'stop after N posts': only the ones still missing
                done = c.execute("SELECT COUNT(*) FROM posts WHERE created >= ?", (state["started"],)).fetchone()[0]
                spec["limit"] = max(1, spec["limit"] - done)
        spec["start_page"] = page
        state.update(resumes=tries, last_resume_page=page)
        try:
            start_manual(spec, resumed=state)
        except HTTPException:
            pass                          # something else is scraping; the saved run stays for the next start-up

    @app.post("/scrape")
    def scrape_start(url: str = Form(...), keywords: str = Form(""),
                     limit: int = Form(20), stop_mode: str = Form("count"), until: str = Form(""),
                     card: str = Form(""), title: str = Form(""),
                     tag: str = Form(""), media: str = Form(""), date: str = Form(""), url_kind: str = Form("listing"),
                     start_page: int = Form(1)):
        single = url_kind == "single"
        kws = [k.strip() for k in re.split(r"[\n,]", keywords) if k.strip()]
        if not kws and not single:
            raise HTTPException(400, "enter at least one keyword")
        if not url.startswith(("http://", "https://")):
            raise HTTPException(400, "URL must start with http:// or https://")
        n, until_iso, stop_text = read_stop(stop_mode, limit, until) if not single else (1, "", "after this one post")
        spec = {"url": url, "keywords": kws, "single": single, "limit": n, "until": until_iso, "stop_text": stop_text,
                "card": card.strip(), "title": title.strip(), "tag": tag.strip(), "media": media.strip(), "date": date.strip(),
                "start_page": 1 if single else max(1, min(start_page, 1_000_000))}
        return RedirectResponse(f"/scrape/{start_manual(spec)}", 303)

    app.state.resume_unfinished = resume_unfinished   # (tests call it directly)
    if scheduler:
        threading.Thread(target=resume_unfinished, daemon=True, name="minibooru-resume").start()

    @app.post("/scrape/{jid}/stop")
    def scrape_stop(jid: str, next: str = Form("")):
        job = jobs.get(jid)
        if not job:
            raise HTTPException(404, "that scrape is no longer running")
        job["cancel"].set()
        return RedirectResponse(safe_next(next, f"/scrape/{jid}") if next else f"/scrape/{jid}", 303)

    @app.post("/scrape-runs/bulk-delete")
    def run_bulk_delete(ids: list[int] = Form([])):
        with core.connect() as c:
            c.executemany("DELETE FROM scrape_runs WHERE id=?", [(i,) for i in ids])
        return RedirectResponse("/scrape", 303)

    @app.post("/scrape-runs/{rid}/delete")
    def run_delete(rid: int):
        with core.connect() as c:
            c.execute("DELETE FROM scrape_runs WHERE id=?", (rid,))
        return RedirectResponse("/scrape", 303)

    @app.post("/watches")
    def watch_add(url: str = Form(...), keywords: str = Form(...), max_posts: int = Form(20),
                  w_mode: str = Form("count"), w_until: str = Form(""),
                  interval_min: int = Form(60), card: str = Form(""), title: str = Form(""),
                  tag: str = Form(""), media: str = Form(""), date: str = Form("")):
        if not url.startswith(("http://", "https://")) or not keywords.strip():
            raise HTTPException(400, "need an http(s) URL and at least one keyword")
        n, until_iso, _ = read_stop(w_mode, max_posts, w_until)
        with core.connect() as c:
            c.execute("""INSERT INTO watches(url, keywords, max_posts, until, interval_min, card, title, tag, media, date, next_run)
                         VALUES (?,?,?,?,?,?,?,?,?,?,0)""",
                      (url.strip(), keywords.strip(), n if not until_iso else 20, until_iso,
                       max(15, interval_min), card.strip(), title.strip(), tag.strip(), media.strip(), date.strip()))
        return RedirectResponse("/scrape", 303)

    @app.post("/watches/{wid}/edit")
    def watch_edit(wid: int, max_posts: int = Form(20), w_mode: str = Form("count"), w_until: str = Form(""),
                   interval_min: int = Form(60)):
        n, until_iso, _ = read_stop(w_mode, max_posts, w_until)
        with core.connect() as c:
            c.execute("UPDATE watches SET max_posts=?, until=?, interval_min=?, next_run=MIN(next_run, ?) WHERE id=?",
                      (n if not until_iso else max(1, min(max_posts, 5000)), until_iso, max(15, interval_min),
                       time.time() + max(15, interval_min) * 60, wid))
        return RedirectResponse("/scrape", 303)

    @app.post("/watches/{wid}/{action}")
    def watch_action(wid: int, action: str):
        with core.connect() as c:
            if action == "toggle":
                c.execute("UPDATE watches SET enabled=1-enabled WHERE id=?", (wid,))
            elif action == "run":
                c.execute("UPDATE watches SET next_run=0, enabled=1 WHERE id=?", (wid,))
            elif action == "stop":       # stop the run in progress; the watch itself stays scheduled
                if watcher.running_wid == wid and watcher.cancel is not None:
                    watcher.cancel.set()
            elif action == "delete":
                if watcher.running_wid == wid and watcher.cancel is not None:
                    watcher.cancel.set()
                c.execute("DELETE FROM watches WHERE id=?", (wid,))
                watcher.logs.pop(wid, None)
            else:
                raise HTTPException(404)
        return RedirectResponse("/scrape", 303)

    @app.get("/watch/{wid}")
    def watch_log(request: Request, wid: int):
        with core.connect() as c:
            w = c.execute("SELECT * FROM watches WHERE id=?", (wid,)).fetchone()
        if not w:
            raise HTTPException(404, "that auto rescan no longer exists")
        running = watcher.running_wid == wid
        job = {"url": w["url"], "keywords": [k.strip() for k in w["keywords"].replace("\n", ",").split(",") if k.strip()],
               "log": watcher.logs.get(wid) or ["No log yet. The log of a run is kept until the server restarts."],
               "running": running, "heading": "Auto rescan", "stop_url": f"/watches/{wid}/stop" if running else None,
               "stop_text": (f"back to {scraper.describe_date(date.fromisoformat(w['until']))}" if w["until"]
                             else f"up to {w['max_posts']} new posts") + ", or at the first post already saved"}
        return tpl.TemplateResponse(request, "scrape.html", {"job": job})

    @app.get("/scrape/{jid}")
    def scrape_status(request: Request, jid: str):
        job = jobs.get(jid)
        if not job:
            raise HTTPException(404)
        return tpl.TemplateResponse(request, "scrape.html", {"job": job, "jid": jid})

    return app


app = create_app() if os.environ.get("MINIBOORU_EAGER") else None

import argparse
import sys

from . import core
from .importers import REGISTRY
from .importers import folder, gallerydl, htmlscrape

ADDERS = {"folder": folder.add_arguments, "gallerydl": gallerydl.add_arguments,
          "htmlscrape": htmlscrape.add_arguments}


def main():
    ap = argparse.ArgumentParser(prog="minibooru")
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("serve")
    s.add_argument("--host", default="127.0.0.1")
    s.add_argument("--port", type=int, default=8090)
    imp = sub.add_parser("import").add_subparsers(dest="importer", required=True)
    for name in REGISTRY:
        ADDERS[name](imp.add_parser(name))
    d = sub.add_parser("dedupe-media", help="remove extra copies of the same picture inside each post (keeps the highest resolution)")
    d.add_argument("--apply", action="store_true", help="actually remove them (without it, only counts)")
    b = sub.add_parser("brand", help="set this install's name and/or icon (same as the first-run setup page)")
    b.add_argument("--name")
    b.add_argument("--icon", help="picture file to use as the icon")
    b.add_argument("--reset-icon", action="store_true", help="go back to the default icon")
    args = ap.parse_args()
    if args.cmd == "serve":
        import uvicorn
        from .web import create_app
        import os
        if not os.environ.get("MINIBOORU_PASSWORD") and args.host not in ("127.0.0.1", "localhost", "::1"):
            print(f"WARNING: no MINIBOORU_PASSWORD set and listening on {args.host}: anyone who can reach this "
                  "address can see and change everything. Set a password.", flush=True)
        uvicorn.run(create_app(scheduler=True), host=args.host, port=args.port)
    elif args.cmd == "brand":
        from pathlib import Path
        with core.connect() as c:
            if args.name is not None:
                core.set_setting(c, "site_name", core.clean_site_name(args.name))
            if args.icon:
                core.set_icon(Path(args.icon).read_bytes())
            if args.reset_icon:
                core.reset_icon()
            info = core.brand_info(c)
        print(f"name: {info['name']} ({'set' if info['configured'] else 'not set yet: the setup page will ask'}) | icon: "
              + ("custom" if info["icon_v"] else "default") + " (restart the app to see a new name)")
    elif args.cmd == "dedupe-media":
        with core.connect() as c:
            pids = [r[0] for r in c.execute("SELECT post_id FROM post_media GROUP BY post_id ORDER BY post_id")]
        posts = files = 0
        for pid in pids:
            with core.connect() as c:          # one short transaction per post, so a running scrape is not blocked
                n = core.collapse_copies(c, pid, apply=args.apply)
            posts += bool(n)
            files += n
        print(f"{'removed' if args.apply else 'would remove'} {files} duplicate file(s) in {posts} of {len(pids)} multi-file posts"
              + ("" if args.apply else ". Run again with --apply to do it."))
    else:
        with core.connect() as c:
            print(REGISTRY[args.importer](c, args))


if __name__ == "__main__":
    sys.exit(main())

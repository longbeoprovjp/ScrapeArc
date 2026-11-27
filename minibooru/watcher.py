"""Hourly (configurable) re-scan of saved watches.

A watch = URL + keywords + pages + interval. A daemon thread started by `serve` wakes every 30s, runs
any enabled watch whose next_run has passed, one at a time. `scrape_lock` is shared with the manual
Scrape form so two scrapes never overlap. If the network is down the watch is simply retried next
interval, with no error recorded.
"""
import os
import threading
import time
from datetime import datetime, timezone

import httpx

from . import core, scraper

scrape_lock = threading.Lock()
current: str | None = None             # what is holding scrape_lock right now (shown on the Scrape page)
running_wid: int | None = None          # id of the watch being run right now, if it is an auto rescan
current_jid: str | None = None          # id of the manual scrape job running right now (its progress page is /scrape/<id>)
cancel: threading.Event | None = None   # set() to stop the auto rescan that is running
logs: dict[int, scraper.RunLog] = {}    # live/last log per watch (kept in memory, lost on restart)
started_at: float | None = None          # time.time() when the scrape holding scrape_lock started
POLL_S = 10
MAX_LOG_LINES = 500                     # lines kept per run log (manual runs too); older lines are dropped
SAFETY_LIMIT = 1_000_000                # post cap used when a run stops by date instead of by count (the date is the real stop)


def parse_roles(text: str) -> list[str]:
    return [r for r in (core.normalize_tag(x) for x in (text or "").split(",")) if r]


def _opts(w, cancel_event=None) -> scraper.Options:
    kws = [k.strip() for k in w["keywords"].replace("\n", ",").split(",") if k.strip()]
    until = w["until"] or ""
    return scraper.Options(keywords=kws, sleep=float(os.environ.get("MINIBOORU_SLEEP", 2)),
                           limit=SAFETY_LIMIT if until else w["max_posts"], card=w["card"],
                           title=w["title"], tag=w["tag"], media=w["media"], date=w["date"], stop_on_known=True,
                           until=until, cancel=cancel_event, kind="auto")


def run_watch(wid: int, log=lambda m: None, cancel_event=None) -> None:
    """Run one watch now (caller holds scrape_lock). Records result and schedules the next run."""
    with core.connect() as c:
        w = c.execute("SELECT * FROM watches WHERE id=?", (wid,)).fetchone()
        if not w:
            return
        ok = True   # False: an error, the site unreachable, or nothing at all found on the page (layout changed?)
        try:
            st = scraper.scrape(c, w["url"], _opts(w, cancel_event), log=log)
            ok = bool(st["cancelled"] or (st["seen"] > 0 and not (st["error"] and not st["new"] and not st["already"])))
            result = (f"{st['new']} new, {st['already']} already had, {st['matched']} matched of {st['seen']} scanned"
                      + (", stopped at a post already saved" if st["stopped"] == 1 else "")
                      + (", reached the date limit" if st["stopped"] == 2 else "")
                      + (", stopped by you" if st["cancelled"] else "")
                      + (f", {st['error']} errors" if st["error"] else ""))
        except (httpx.TransportError, OSError):
            result, ok = "site unreachable (offline?), will retry next interval", False
            log(result)
        except Exception as e:  # never let one watch kill the scheduler
            result, ok = f"failed: {e}", False
            log(result)
        now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M")
        row = c.execute("SELECT interval_min FROM watches WHERE id=?", (wid,)).fetchone()   # may have been edited meanwhile
        c.execute("UPDATE watches SET last_run=?, last_result=?, next_run=? WHERE id=?",
                  (now, result, time.time() + (row[0] if row else w["interval_min"]) * 60, wid))
        if ok:
            c.execute("UPDATE watches SET fails=0, fail_since='' WHERE id=?", (wid,))
        else:   # counted so the gallery can say when a rescan has been broken for a while (core.failing_watches)
            c.execute("UPDATE watches SET fails=fails+1, fail_since=CASE WHEN fail_since='' THEN ? ELSE fail_since END"
                      " WHERE id=?", (now, wid))


def run_due() -> int:
    global current, running_wid, cancel, started_at
    n = 0
    with core.connect() as c:
        due = [r[0] for r in c.execute("SELECT id FROM watches WHERE enabled=1 AND next_run<=? ORDER BY next_run",
                                       (time.time(),))]
    for wid in due:
        if not scrape_lock.acquire(blocking=False):
            break  # a manual scrape is running; try again next poll
        try:
            with core.connect() as c:
                row = c.execute("SELECT url FROM watches WHERE id=?", (wid,)).fetchone()
            if not row:
                continue
            current, running_wid, cancel = f"auto rescan of {row[0]}", wid, threading.Event()
            lines = logs[wid] = scraper.RunLog(MAX_LOG_LINES)
            started_at = lines.started
            lines.append(f"Auto rescan started {datetime.now(timezone.utc):%Y-%m-%d %H:%M:%S} UTC")
            try:
                run_watch(wid, lines.append, cancel)
            finally:
                lines.finished = time.time()
            n += 1
        finally:
            current, running_wid, cancel, started_at = None, None, None, None
            scrape_lock.release()
    return n


def start(stop: threading.Event | None = None) -> threading.Thread:
    stop = stop or threading.Event()

    def loop():
        while not stop.wait(POLL_S):
            try:
                run_due()
            except Exception:
                pass

    t = threading.Thread(target=loop, daemon=True, name="minibooru-watcher")
    t.start()
    return t

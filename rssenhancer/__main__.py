"""RssEnhancer entry point.

Runs the publisher (a small web server) and, in parallel, whichever sources are configured:
  subscriptions  mail to SUBSCRIPTION_ADDRESS  -> AI-split RSS items            (config.toml)
  hydrator       third-party feeds (config.toml) -> the same feeds with full text
The sources never talk to each other; each has its own thread, schedule and state.

Settings are read again while the program runs:
  config.toml             is checked for changes; a valid edit restarts the process
                         within seconds. A config that cannot be used is reported and the previous one stays.
  the keyword list file is re-read before every pass.
  config.toml             a change is checked first (--check, in a separate process); if the new settings are usable
                         the program finishes its running passes and restarts itself in place, otherwise it says
                         what is wrong and keeps running with the old settings.

  python3 -m rssenhancer            run forever
  python3 -m rssenhancer --once     refresh every source once, write the feeds, exit (no web server)
  python3 -m rssenhancer --check    only check that the settings are usable (exit status 0 or 1), change nothing
  python3 -m rssenhancer --only hydrator
"""
import argparse
import atexit
import os
import signal
import subprocess
import sys
import threading
import time

from . import ai, common, feed_hydrator, publisher, subscription_processor
from .common import log

COMPONENTS = {
    "subscriptions": (subscription_processor, "set subscriptions.address and subscriptions.imap.password in config.toml"),
    "hydrator": (feed_hydrator, "add at least one [[hydrator.feeds]] entry to config.toml"),
}
WATCH_SECONDS = 3


def _run(name, worker, once, stop):
    while not stop.is_set():
        try:
            worker.step()
        except Exception as e:
            log("failed: %s" % e)
        if once:
            return
        deadline = time.monotonic() + worker.interval()
        while not stop.is_set():
            left = deadline - time.monotonic()
            if left <= 0:
                break
            if worker.changed and worker.changed():
                log("its settings changed, running now")
                break
            stop.wait(min(left, WATCH_SECONDS))


def _restart_if_usable(stop, threads, only):
    """The settings file changed: check the new settings in a separate process and, only if they are usable, finish
    the running passes and replace this process with a fresh one. Returns only when the new settings are not usable."""
    clean = os.environ.copy()
    cmd = [sys.executable, "-m", "rssenhancer", "--check"] + [x for o in only for x in ("--only", o)]
    try:
        r = subprocess.run(cmd, cwd=str(common.ROOT), env=clean, capture_output=True, text=True, timeout=180)
    except subprocess.TimeoutExpired:
        log("checking the new settings took too long; keeping the old ones")
        return
    if r.returncode != 0:
        tail = (r.stderr.strip() or r.stdout.strip()).splitlines()[-3:]
        log("the new settings cannot be used, keeping the old ones: %s" % " | ".join(tail))
        return
    log("settings changed: restarting (passes that are running finish first)")
    stop.set()
    for t in threads:
        t.join()
    ai.stop_rapid_mlx()
    sys.stderr.flush()
    os.execv(sys.executable, [sys.executable, "-m", "rssenhancer"] + sys.argv[1:])


def _watch(stop, threads, only, hydrator_running):
    paths = [common.config.CONFIG_PATH]
    sig = lambda: tuple(common.file_signature(p) for p in paths)
    last = sig()
    while not stop.wait(WATCH_SECONDS):
        now = sig()
        if now == last:
            continue
        time.sleep(1.0)  # an editor may still be writing
        now = sig()
        last = now  # judged once per edit, so a broken file is not checked again and again
        _restart_if_usable(stop, threads, only)


def main(argv=None):
    ap = argparse.ArgumentParser(prog="rssenhancer", description="Publish AI-split newsletters and full-text feeds as local RSS.")
    ap.add_argument("--once", action="store_true", help="refresh every source once and exit (no web server)")
    ap.add_argument("--check", action="store_true", help="check that the settings are usable and exit")
    ap.add_argument("--only", action="append", choices=sorted(COMPONENTS), help="run only this source (repeatable)")
    args = ap.parse_args(argv)
    nothing = "Nothing to do. Configure at least one source: " + "; ".join("%s: %s" % (n, h) for n, (_, h) in COMPONENTS.items())

    if args.check:
        checked = 0
        for name, (module, _hint) in COMPONENTS.items():
            if args.only and name not in args.only:
                continue
            if module.configured():
                module.validate()
                print("%s: the settings are usable" % name)
                checked += 1
        if not checked:
            sys.exit(nothing)
        return

    common.prepare_data_dir()
    atexit.register(ai.stop_rapid_mlx)  # never leave a rapid-mlx server we started behind
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))  # lets atexit run under launchd

    workers = []
    for name, (module, hint) in COMPONENTS.items():
        if args.only and name not in args.only:
            continue
        if not module.configured():
            log("%s: not configured (%s)" % (name, hint))
            continue
        workers.append((name, module.make_worker()))
    if not workers:
        sys.exit(nothing)

    if not args.once:
        publisher.serve()
        log("publisher: %s/ (listening on %s:%d)" % (common.BASE, common.HOST, common.PORT))

    stop = threading.Event()
    threads = [threading.Thread(target=_run, args=(n, w, args.once, stop), name=n, daemon=True) for n, w in workers]
    for t in threads:
        t.start()
    try:
        if args.once:
            for t in threads:
                t.join()
        else:
            _watch(stop, threads, args.only or [], any(n == "hydrator" for n, _ in workers))
    except KeyboardInterrupt:
        pass
    finally:
        stop.set()


if __name__ == "__main__":
    main()

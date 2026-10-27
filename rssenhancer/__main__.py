"""RssEnhancer entry point.

Runs the publisher (a small web server) and, in parallel, whichever sources are configured:
  subscriptions  mail to SUBSCRIPTION_ADDRESS  -> AI-split RSS items            (.env)
  hydrator       third-party feeds in config.json -> same feeds with full text   (config.json)
The sources never talk to each other; each has its own thread, schedule and state.

  python3 -m rssenhancer            run forever
  python3 -m rssenhancer --once     refresh every source once, write the feeds, exit (no web server)
  python3 -m rssenhancer --only hydrator
"""
import argparse
import atexit
import signal
import sys
import threading

from . import ai, common, feed_hydrator, publisher, subscription_processor
from .common import log

COMPONENTS = {
    "subscriptions": (subscription_processor, "set SUBSCRIPTION_ADDRESS (and IMAP_PASSWORD) in .env"),
    "hydrator": (feed_hydrator, "create config.json with a \"feeds\" list (see config.example.json)"),
}


def _run(name, step, interval, once, stop):
    while not stop.is_set():
        try:
            step()
        except Exception as e:
            log("failed: %s" % e)
        if once:
            return
        stop.wait(interval)


def main(argv=None):
    ap = argparse.ArgumentParser(prog="rssenhancer", description="Publish AI-split newsletters and full-text feeds as local RSS.")
    ap.add_argument("--once", action="store_true", help="refresh every source once and exit (no web server)")
    ap.add_argument("--only", action="append", choices=sorted(COMPONENTS), help="run only this source (repeatable)")
    args = ap.parse_args(argv)

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
        step, interval = module.make_worker()
        workers.append((name, step, interval))
    if not workers:
        sys.exit("Nothing to do. Configure at least one source: " + "; ".join("%s: %s" % (n, h) for n, (_, h) in COMPONENTS.items()))

    if not args.once:
        publisher.serve()
        log("publisher: %s/ (listening on %s:%d)" % (common.BASE, common.HOST, common.PORT))

    stop = threading.Event()
    threads = [threading.Thread(target=_run, args=(n, s, i, args.once, stop), name=n, daemon=True) for n, s, i in workers]
    for t in threads:
        t.start()
    try:
        if args.once:
            for t in threads:
                t.join()
        else:
            stop.wait()
    except KeyboardInterrupt:
        pass
    finally:
        stop.set()


if __name__ == "__main__":
    main()

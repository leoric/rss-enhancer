"""Shared plumbing: TOML configuration, logging, HTTP helpers and the data directory.

Nothing in here knows about e-mail or about feeds; the publisher, the subscription processor and the
feed hydrator all build on it.
"""
import ipaddress
import json
import re
import socket
import sys
import threading
import time
import urllib.error
import urllib.request
import zlib
from pathlib import Path

from . import config
from typing import NamedTuple
from urllib.parse import urljoin, urlparse

ROOT = Path(__file__).resolve().parent.parent  # project root: config.toml lives here
UA = "Mozilla/5.0 (compatible; RssEnhancer/1.0)"

def env(name, default=None):
    return config.setting(name, default)


def file_signature(path):
    """(mtime, size) of a file, or None when it does not exist: enough to notice an edit."""
    try:
        st = Path(path).stat()
        return st.st_mtime_ns, st.st_size
    except OSError:
        return None


class Worker(NamedTuple):
    """What a source hands to the orchestrator."""
    step: object            # one pass
    interval: object        # callable -> seconds until the next pass (re-read after every pass)
    changed: object = None  # callable -> True when a file this source reads has changed: run the next pass now




PORT = int(env("RSSENHANCER_PORT", 8787))
HOST = env("RSSENHANCER_HOST", "127.0.0.1")
BASE = (env("RSSENHANCER_PUBLIC_URL") or "http://127.0.0.1:%d" % PORT).rstrip("/")
DATA = Path(env("RSSENHANCER_DIR", "~/Library/Application Support/RssEnhancer")).expanduser()
LOG_DIR = Path(env("RSSENHANCER_LOG_DIR", "~/Library/Logs/RssEnhancer")).expanduser()  # RssEnhancer.log (launchd), stats.log, rapid-mlx.log



def _source():
    """Where a line belongs when the caller gave no tag: the worker's name, `main` for the main thread, `app`
    for any other anonymous thread. A line always has a tag."""
    name = threading.current_thread().name
    if name == "MainThread":
        return "main"
    return "app" if name.startswith("Thread-") else name


def log(*a, level="INFO", tag=None):
    """One line on stderr: `LEVEL HH:MM:SS [source] message`. The level comes first so lines can be grepped
    or sorted by it. The source is `tag` when given, otherwise the thread's name (see _source); HTTP request
    threads are anonymous, so the publisher passes tag="publisher"."""
    print("%-5s %s [%s]" % (level, time.strftime("%H:%M:%S"), tag or _source()), *a, file=sys.stderr, flush=True)


def warn(*a, **kw):
    log(*a, level="WARN", **kw)


def error(*a, **kw):
    log(*a, level="ERROR", **kw)


_stats_lock = threading.Lock()


def stats(message):
    """Append a line to stats.log in the log directory (with the date, since the file outlives runs).
    Used for numbers about how well things work, e.g. how much the summaries shrink the text; the same
    line is meant to go to the normal log too, this file only collects them in one place."""
    line = "%s [%s] %s\n" % (time.strftime("%Y-%m-%d %H:%M:%S"), _source(), message)
    try:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        with _stats_lock, open(LOG_DIR / "stats.log", "a", encoding="utf-8") as f:
            f.write(line)
    except OSError as e:
        warn("cannot write stats.log: %s" % e)


def prepare_data_dir():
    """Create the data directory layout and the log directory."""
    for sub in ("feeds", "state", "state/hydrator"):
        (DATA / sub).mkdir(parents=True, exist_ok=True)
    LOG_DIR.mkdir(parents=True, exist_ok=True)


def atomic_write(path, data):
    path = Path(path)
    tmp = path.with_name(path.name + ".tmp")
    if isinstance(data, str):
        data = data.encode("utf-8")
    tmp.write_bytes(data)
    tmp.replace(path)


class Response(NamedTuple):
    status: int
    headers: object
    body: bytes
    url: str  # final URL after redirects


def check_public_url(url):
    """Raise unless url is http(s) and every address its host resolves to is publicly routable.
    Links in someone else's feed must not be able to make us read the router, localhost or the LAN."""
    p = urlparse(url)
    if p.scheme not in ("http", "https") or not p.hostname:
        raise RuntimeError("refusing to fetch %s: only http(s) URLs are allowed" % url)
    try:
        infos = socket.getaddrinfo(p.hostname, p.port or (443 if p.scheme == "https" else 80), proto=socket.IPPROTO_TCP)
    except socket.gaierror as e:
        raise RuntimeError("cannot resolve %s: %s" % (p.hostname, e))
    for info in infos:
        ip = ipaddress.ip_address(info[4][0].split("%")[0])
        if getattr(ip, "ipv4_mapped", None):
            ip = ip.ipv4_mapped
        if not ip.is_global:
            raise RuntimeError("refusing to fetch %s: %s is not a public address" % (url, ip))


class _GuardedRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        check_public_url(urljoin(req.full_url, newurl))  # every hop, not just the first URL
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def http_get(url, headers=None, timeout=30, max_bytes=8_000_000, allow_private=True):
    """GET with gzip support and a size cap. A 304 comes back as a normal Response; other errors raise.
    allow_private=False refuses loopback/private/link-local addresses, also after redirects."""
    hdrs = {"User-Agent": UA, "Accept-Encoding": "gzip"}
    hdrs.update(headers or {})
    req = urllib.request.Request(url, headers=hdrs)
    try:
        if allow_private:
            r = urllib.request.urlopen(req, timeout=timeout)
        else:
            check_public_url(url)
            r = urllib.request.build_opener(_GuardedRedirects).open(req, timeout=timeout)
    except urllib.error.HTTPError as e:
        if e.code == 304:
            return Response(304, e.headers, b"", url)
        raise RuntimeError("HTTP %d for %s" % (e.code, url))
    with r:
        body = r.read(max_bytes + 1)
        if (r.headers.get("Content-Encoding") or "").lower() == "gzip":
            body = zlib.decompressobj(16 + zlib.MAX_WBITS).decompress(body, max_bytes + 1)
        if len(body) > max_bytes:
            raise RuntimeError("response from %s is larger than %d bytes" % (url, max_bytes))
        return Response(r.status, r.headers, body, r.geturl())


def post_json(url, headers, body, timeout=180):
    headers = dict(headers)
    headers.setdefault("User-Agent", "RssEnhancer/1.0")
    req = urllib.request.Request(url, data=json.dumps(body).encode(), headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        raise RuntimeError("HTTP %d from %s: %s" % (e.code, url, e.read()[:500].decode("utf-8", "replace")))


def compile_keywords(items):
    """Keywords (of titles, authors) -> [(keyword as written, compiled regex)]. Matching ignores case.
      sponsor       a whole word (not "sponsorship")
      ad*           a word that starts with it (advertisement, ads, ...); * may also stand in the middle or at the start
      read more     a phrase, with any spacing between its words
      /regex/i      a regular expression searched in the title, with flags i, s, m"""
    rules = []
    for item in items:
        item = item.strip()
        if not item:
            continue
        m = re.fullmatch(r"/(.+)/([a-z]*)", item, re.S)
        try:
            if m:
                flags = (re.I if "i" in m.group(2) else 0) | (re.S if "s" in m.group(2) else 0) | (re.M if "m" in m.group(2) else 0)
                rx = re.compile(m.group(1), flags)
            else:
                body = re.escape(item).replace(r"\*", r"\w*").replace(r"\ ", r"\s+")
                rx = re.compile(r"(?<!\w)%s(?!\w)" % body, re.I)
        except re.error as e:
            raise ValueError("bad keyword %r: %s" % (item, e))
        rules.append((item, rx))
    return rules

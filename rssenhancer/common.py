"""Shared plumbing: configuration (.env + environment), logging, HTTP helpers and the data directory.

Nothing in here knows about e-mail or about feeds; the publisher, the subscription processor and the
feed hydrator all build on it.
"""
import ipaddress
import json
import os
import shutil
import socket
import sys
import threading
import time
import urllib.error
import urllib.request
import zlib
from pathlib import Path
from typing import NamedTuple
from urllib.parse import urljoin, urlparse

ROOT = Path(__file__).resolve().parent.parent  # project root: .env and config.json live here
UA = "Mozilla/5.0 (compatible; RssEnhancer/1.0)"

# Names used before the project was called RssEnhancer still work; the new name wins when both are set.
_LEGACY = {
    "RSSENHANCER_PORT": "MAILRSS_PORT",
    "RSSENHANCER_HOST": "MAILRSS_HOST",
    "RSSENHANCER_PUBLIC_URL": "MAILRSS_PUBLIC_URL",
    "RSSENHANCER_DIR": "MAILRSS_DIR",
    "RSSENHANCER_ENV": "MAILRSS_ENV",
    "RSSENHANCER_ACCESS_LOG": "MAILRSS_ACCESS_LOG",
    "SUBSCRIPTION_ADDRESS": "MAILRSS_ADDRESS",
    "SUBSCRIPTION_FEED_TITLE": "MAILRSS_FEED_TITLE",
    "SUBSCRIPTION_POLL_MINUTES": "POLL_MINUTES",
    "SUBSCRIPTION_LOOKBACK_DAYS": "LOOKBACK_DAYS",
}


DOTENV_KEYS = set()  # variables that came from .env, not from the real environment (cleared before a restart)


def env_file_path():
    explicit = os.environ.get("RSSENHANCER_ENV") or os.environ.get("MAILRSS_ENV")
    return Path(explicit) if explicit else ROOT / ".env"


def load_dotenv():
    """Load KEY=VALUE lines from .env in the project root (or the file named by RSSENHANCER_ENV).
    Variables already present in the real environment win. A value in quotes may run over several lines,
    which is how a whole JSON config fits into one variable:
        RSSENHANCER_CONFIG_JSON='{
          "feeds": [...]
        }'"""
    path = env_file_path()
    if not path.is_file():
        return
    lines = path.read_text(encoding="utf-8").splitlines()
    i = 0
    while i < len(lines):
        line = lines[i].strip()
        i += 1
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, val = line.split("=", 1)
        key, val = key.strip(), val.strip()
        if key.startswith("export "):
            key = key[7:].strip()
        if val[:1] in ("'", '"') and not (len(val) >= 2 and val[-1] == val[0]):
            quote, parts = val[0], [val[1:]]  # the closing quote is the last character of some later line
            while i < len(lines):
                nxt = lines[i]
                i += 1
                if nxt.rstrip().endswith(quote):
                    parts.append(nxt.rstrip()[:-1])
                    break
                parts.append(nxt)
            val = "\n".join(parts)
        elif len(val) >= 2 and val[0] == val[-1] and val[0] in "\"'":
            val = val[1:-1]
        elif " #" in val:
            val = val.split(" #")[0].rstrip()
        if key not in os.environ:
            os.environ[key] = val
            DOTENV_KEYS.add(key)


load_dotenv()


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


def env(name, default=None):
    v = os.environ.get(name, "").strip()
    if not v and name in _LEGACY:
        v = os.environ.get(_LEGACY[name], "").strip()
    return v or default


PORT = int(env("RSSENHANCER_PORT") or env("PORT") or "8787")
HOST = env("RSSENHANCER_HOST", "127.0.0.1")
BASE = (env("RSSENHANCER_PUBLIC_URL") or "http://127.0.0.1:%d" % PORT).rstrip("/")
DATA = Path(env("RSSENHANCER_DIR", "~/Library/Application Support/RssEnhancer")).expanduser()


def log(*a):
    name = threading.current_thread().name
    tag = "" if name == "MainThread" or name.startswith("Thread-") else " [%s]" % name
    print(time.strftime("%H:%M:%S") + tag, *a, file=sys.stderr, flush=True)


def prepare_data_dir():
    """Create the data directory layout, adopting data left by the pre-rename mailrss version."""
    legacy = Path("~/Library/Application Support/mailrss").expanduser()
    if not env("RSSENHANCER_DIR") and not DATA.exists() and legacy.exists():
        DATA.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(legacy), str(DATA))
        log("moved the old data directory %s to %s" % (legacy, DATA))
    for sub in ("feeds", "state", "state/hydrator"):
        (DATA / sub).mkdir(parents=True, exist_ok=True)
    for old, new in (("state.json", "state/subscriptions.json"), ("feed.xml", "feeds/mail.feed.xml")):
        if (DATA / old).exists() and not (DATA / new).exists():
            (DATA / old).replace(DATA / new)


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

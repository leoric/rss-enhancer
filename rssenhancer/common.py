"""Shared plumbing: TOML configuration, logging, HTTP helpers and the data directory.

Nothing in here knows about e-mail or about feeds; the publisher, the subscription processor and the
feed hydrator all build on it.
"""
import ipaddress
import json
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



def log(*a):
    name = threading.current_thread().name
    tag = "" if name == "MainThread" or name.startswith("Thread-") else " [%s]" % name
    print(time.strftime("%H:%M:%S") + tag, *a, file=sys.stderr, flush=True)


def prepare_data_dir():
    """Create the data directory layout."""
    for sub in ("feeds", "state", "state/hydrator"):
        (DATA / sub).mkdir(parents=True, exist_ok=True)


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

"""Publisher: stores finished feeds as files and serves them over HTTP.

The sources (subscription processor, feed hydrator) never touch the web server; they hand a finished feed
to publish() under a file name, and the server hands out every `*.feed.xml` it finds in the feeds directory.
"""
import datetime as dt
import hashlib
import html
import re
import threading
from email.utils import format_datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import quote, unquote

from . import common
from .common import atomic_write, env, log

MAIL_FEED = "mail.feed.xml"          # the subscription processor's feed
NAME_RE = re.compile(r"^[A-Za-z0-9._-]{1,200}\.feed\.xml$")


def feeds_dir():
    return common.DATA / "feeds"


def valid_name(name):
    return bool(NAME_RE.match(name)) and ".." not in name


def public_url(name):
    return "%s/%s" % (common.BASE, quote(name))


def exists(name):
    return (feeds_dir() / name).is_file()


def publish(name, data):
    """Store a finished feed (str or bytes) so the server serves it from now on."""
    if not valid_name(name):
        raise ValueError("invalid feed file name %r (must look like something.feed.xml)" % name)
    feeds_dir().mkdir(parents=True, exist_ok=True)
    atomic_write(feeds_dir() / name, data)


def rss_document(title, description, items, self_name):
    """Build an RSS 2.0 document from items: dicts with guid, title, html, ts and optional author."""
    esc = html.escape
    now = format_datetime(dt.datetime.now(dt.timezone.utc))
    out = ['<?xml version="1.0" encoding="UTF-8"?>',
           '<rss version="2.0" xmlns:atom="http://www.w3.org/2005/Atom" xmlns:dc="http://purl.org/dc/elements/1.1/"><channel>',
           "<title>%s</title>" % esc(title), "<link>%s/</link>" % common.BASE,
           '<atom:link href="%s" rel="self" type="application/rss+xml"/>' % esc(public_url(self_name)),
           "<description>%s</description>" % esc(description),
           "<lastBuildDate>%s</lastBuildDate>" % now]
    for it in items:
        pub = format_datetime(dt.datetime.fromtimestamp(it["ts"], dt.timezone.utc))
        body = it["html"].replace("]]>", "]]]]><![CDATA[>")
        creator = "<dc:creator>%s</dc:creator>" % esc(it["author"]) if it.get("author") else ""
        out.append('<item><title>%s</title><link>%s/#%s</link><guid isPermaLink="false">%s</guid>'
                   "<pubDate>%s</pubDate>%s<description><![CDATA[%s]]></description></item>"
                   % (esc(it["title"]), common.BASE, quote(it["guid"], safe=""), esc(it["guid"]), pub, creator, body))
    out.append("</channel></rss>")
    return "\n".join(out)


def summary_block(inner_html, label="Summary", rule="after"):
    """A summary as a block inside an item's content. Many readers show only the content (or only the summary)
    of an entry, so the summary is put inside the content. The AI summaries of the subscription processor and
    the summaries moved by the feed hydrator share this look: a labelled box and a rule that separates it from
    the text (rule="after" for a summary at the start, "before" for one at the end). An empty label gives
    plain text without the box."""
    if not inner_html.strip():
        return ""
    box = "<blockquote><strong>%s:</strong>%s</blockquote>" % (html.escape(label), inner_html) if label else inner_html
    return box + "<hr/>" if rule == "after" else "<hr/>" + box


def _content_type(data):
    head = data[:2048]
    if b"<rss" not in head and b"<feed" in head:
        return "application/atom+xml; charset=utf-8"
    return "application/rss+xml; charset=utf-8"


class FeedHandler(BaseHTTPRequestHandler):
    """Serves `/` (an index) and `/<name>.feed.xml`. Everything else (state, logs, ...) is a 404."""

    def _reply(self, status, body=b"", ctype="text/plain; charset=utf-8", head_only=False, extra=None):
        self.send_response(status)
        if status != 304:
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if body and not head_only and status != 304:
            self.wfile.write(body)

    def _send(self, head_only):
        path = unquote(self.path.split("?")[0])
        if path == "/":
            return self._index(head_only)
        name = path.lstrip("/")
        if not valid_name(name):
            return self._reply(404, b"not found\n", head_only=head_only)
        try:
            data = (feeds_dir() / name).read_bytes()
        except OSError:
            return self._reply(404, b"feed not generated yet\n", head_only=head_only)
        etag = '"%s"' % hashlib.md5(data).hexdigest()[:20]
        if self.headers.get("If-None-Match") == etag:
            return self._reply(304, extra={"ETag": etag})
        self._reply(200, data, _content_type(data), head_only, {"ETag": etag, "Cache-Control": "no-cache"})

    def _index(self, head_only):
        rows = []
        for p in sorted(feeds_dir().glob("*.feed.xml")):
            rows.append('<li><a href="%s">%s</a></li>' % (quote(p.name), html.escape(p.name)))
        page = ("<!doctype html><meta charset=utf-8><title>RssEnhancer</title><h1>RssEnhancer feeds</h1><ul>%s</ul>"
                % "".join(rows)).encode("utf-8")
        self._reply(200, page, "text/html; charset=utf-8", head_only)

    def do_GET(self):
        self._send(False)

    def do_HEAD(self):
        self._send(True)

    def log_message(self, fmt, *args):
        if len(args) == 3 and bool(env("RSSENHANCER_ACCESS_LOG", True)):  # (request line, status, size)
            log("http %s -> %s from %s (UA: %s)" % (args[0], args[1], self.client_address[0],
                                                    self.headers.get("User-Agent", "-")))


def serve():
    """Start the web server in a background thread and return it."""
    srv = ThreadingHTTPServer((common.HOST, common.PORT), FeedHandler)
    threading.Thread(target=srv.serve_forever, name="publisher", daemon=True).start()
    return srv

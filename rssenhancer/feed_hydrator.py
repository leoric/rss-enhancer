"""Feed hydrator: turns teaser feeds into full-text feeds.

For every configured RSS/Atom feed it
  1. fetches the feed (conditional GET: unchanged feeds cost one 304 response),
  2. checks, without touching the entries' contents, whether any entry is new,
  3. for new entries only: opens the article page and extracts the full text with a CSS selector,
  4. puts that text into the feed (Atom: replaces <content>; RSS: keeps <description> and adds
     <content:encoded>) and hands the result to the publisher as `<domain>-<path>.feed.xml`.

Hydrated article text is cached in a state file, so old entries are never fetched or parsed again.
The hydrator knows nothing about e-mail; it only needs config.json (see config.example.json).

A feed's "selector" is a CSS-like selector for sites you know, or "auto" for feeds that link to arbitrary
sites (Hacker News and the like): then the main text of every page is found heuristically ("reader mode").
"""
import html
import io
import json
import re
import time
import xml.etree.ElementTree as ET
from pathlib import Path
from urllib.parse import urlparse

from . import common, publisher
from .common import ROOT, atomic_write, env, http_get, log
from .htmlutil import clean_fragment, decode_html, extract_article, extract_html, parse_selector

ATOM = "http://www.w3.org/2005/Atom"
CONTENT = "http://purl.org/rss/1.0/modules/content/"
SUMMARY_MODES = ("keep", "start", "end")
VIEW_FORMAT = 3  # bump when the look of the published entries changes, so existing feeds are rewritten
MAX_TRIES = 3  # an article that cannot be hydrated is retried this many times, then left as it is
AUTO = "auto"
DEFAULTS = {"poll_minutes": 60, "request_delay": 1.0, "max_items_per_run": 30, "timeout": 30, "allow_private_hosts": False}
# sites whose pages are no articles (used with "auto" unless the feed sets its own "skip_hosts")
DEFAULT_SKIP_HOSTS = ["youtube.com", "youtu.be", "vimeo.com", "twitch.tv", "twitter.com", "x.com", "instagram.com",
                      "facebook.com", "tiktok.com", "linkedin.com"]


class Skip(Exception):
    """This entry will never be hydrated (not a web page, video site, ...): do not retry."""


# ------------------------------------------------------------------ configuration
def config_path():
    return Path(env("RSSENHANCER_CONFIG") or ROOT / "config.json")


def load_config():
    """Parsed and validated config, or None when there is no config file."""
    p = config_path()
    if not p.is_file():
        return None
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
    except ValueError as e:
        raise SystemExit("%s is not valid JSON: %s" % (p, e))
    cfg = dict(DEFAULTS)
    cfg.update({k: v for k, v in raw.items() if k != "feeds" and not k.startswith("_")})
    feeds = []
    for i, f in enumerate(raw.get("feeds") or [], 1):
        where = "%s: feed #%d" % (p.name, i)
        if not isinstance(f, dict) or not re.match(r"https?://", str(f.get("url", ""))):
            raise SystemExit("%s needs an http(s) \"url\"" % where)
        if not f.get("selector"):
            raise SystemExit("%s (%s) needs a \"selector\", e.g. \"div.itemFullText\"" % (where, f["url"]))
        if f["selector"] != AUTO:
            try:
                parse_selector(f["selector"])
            except ValueError as e:
                raise SystemExit("%s (%s): %s" % (where, f["url"], e))
        if not isinstance(f.get("drop_unhydrated", True), bool):
            raise SystemExit("%s (%s): \"drop_unhydrated\" must be true or false" % (where, f["url"]))
        if "prepend_description" in f:
            raise SystemExit("%s (%s): \"prepend_description\" was replaced by \"summary\": \"keep\", \"start\" or \"end\"" % (where, f["url"]))
        if f.get("summary", "keep") not in SUMMARY_MODES:
            raise SystemExit("%s (%s): \"summary\" must be one of %s" % (where, f["url"], ", ".join(SUMMARY_MODES)))
        if not isinstance(f.get("summary_label", ""), str):
            raise SystemExit("%s (%s): \"summary_label\" must be a string" % (where, f["url"]))
        if not isinstance(f.get("skip_hosts", []), list) or not isinstance(f.get("min_words", 0), int):
            raise SystemExit("%s (%s): \"skip_hosts\" must be a list and \"min_words\" a number" % (where, f["url"]))
        if f.get("link_from", "link") not in ("link", "id"):
            raise SystemExit("%s (%s): \"link_from\" must be \"link\" or \"id\"" % (where, f["url"]))
        if f.get("name") and not publisher.valid_name(f["name"]):
            raise SystemExit("%s (%s): \"name\" must look like something.feed.xml" % (where, f["url"]))
        feeds.append(f)
    cfg["feeds"] = feeds
    return cfg


def configured():
    cfg = load_config()
    return bool(cfg and cfg["feeds"])


def feed_filename(url, override=None):
    """https://alternatio.org/articles/articles?format=feed&type=atom -> alternatio.org-articles-articles.feed.xml
    (<domain>-<path>, a trailing .xml/.rss/.atom/.rdf extension and the query string are dropped)."""
    if override:
        return override
    u = urlparse(url)
    path = re.sub(r"\.(xml|rss|atom|rdf)$", "", u.path.strip("/"), flags=re.I)
    name = (u.hostname or "feed") + ("-" + path.replace("/", "-") if path else "")
    return re.sub(r"[^A-Za-z0-9._-]", "_", name)[:190] + ".feed.xml"


# ------------------------------------------------------------------ reading feeds
def _local(tag):
    return tag.rsplit("}", 1)[-1]


def _text(el):
    return (el.text or "").strip() if el is not None else ""


def detect_format(root_tag):
    if root_tag == "{%s}feed" % ATOM:
        return "atom"
    if root_tag == "rss":
        return "rss"
    raise ValueError("unsupported feed format (root element %r); only Atom and RSS 2.0 are handled" % root_tag)


def _atom_link(el):
    links = el.findall("{%s}link" % ATOM)
    for link in links:
        if link.get("rel", "alternate") == "alternate" and link.get("href"):
            return link.get("href").strip()
    return (links[0].get("href") or "").strip() if links else ""


def entry_key(el, fmt):
    """Stable identity of an entry: Atom <id> (or link), RSS <guid> (or <link>)."""
    if fmt == "atom":
        return _text(el.find("{%s}id" % ATOM)) or _atom_link(el) or None
    return _text(el.find("guid")) or _text(el.find("link")) or None


def article_url(el, fmt, link_from):
    """Where the full article lives. link_from: "link" (Atom <link rel=alternate href>, RSS <link>) or "id"
    (Atom <id>, RSS <guid>) for feeds that keep the article URL there."""
    if link_from == "id":
        url = _text(el.find("{%s}id" % ATOM)) if fmt == "atom" else _text(el.find("guid"))
    else:
        url = _atom_link(el) if fmt == "atom" else _text(el.find("link"))
    return url if re.match(r"https?://", url or "") else None


def _check_safe(body):
    if re.search(rb"<!ENTITY", body[:20000]):
        raise ValueError("the feed declares XML entities; refusing to parse it")


def scan_keys(body):
    """Keys of all entries, read with a streaming parser that discards each entry right away.
    This is the cheap 'is there anything new?' check: nothing but ids is looked at."""
    _check_safe(body)
    fmt, keys = None, []
    entry_tag = None
    for event, el in ET.iterparse(io.BytesIO(body), events=("start", "end")):
        if event == "start":
            if fmt is None:
                fmt = detect_format(el.tag)
                entry_tag = "{%s}entry" % ATOM if fmt == "atom" else "item"
        elif el.tag == entry_tag:
            k = entry_key(el, fmt)
            if k:
                keys.append(k)
            el.clear()
    return fmt, keys


def parse_feed(body):
    """Full parse. Namespace prefixes used in the document are registered so they survive serialisation."""
    _check_safe(body)
    it = ET.iterparse(io.BytesIO(body), events=("start-ns",))
    for _, (prefix, uri) in it:
        if prefix and not re.fullmatch(r"ns\d+", prefix):
            try:
                ET.register_namespace(prefix, uri)
            except ValueError:
                pass
    root = it.root
    return root, detect_format(root.tag)


def entries_of(root, fmt):
    if fmt == "atom":
        return root.findall("{%s}entry" % ATOM)
    channel = root.find("channel")
    return channel.findall("item") if channel is not None else []


# ------------------------------------------------------------------ editing feeds
def original_html(el, fmt):
    """The entry's own summary/description as clean HTML (used to keep it above the full text)."""
    if fmt == "atom":
        s = el.find("{%s}summary" % ATOM)
        text = (s.text or "") if s is not None else ""
        is_html = s is not None and s.get("type") in ("html", "xhtml")
    else:
        d = el.find("description")
        text = (d.text or "") if d is not None else ""
        is_html = "<" in text
    if not text.strip():
        return ""
    return clean_fragment(text) if is_html else "<p>%s</p>" % html.escape(text.strip())


def remove_summary(el, fmt):
    """Delete the entry's own summary/description (it has been moved into the content)."""
    if fmt == "atom":
        summary = el.find("{%s}summary" % ATOM)
        if summary is not None:
            el.remove(summary)
    else:
        desc = el.find("description")
        if desc is not None and el.find("title") is not None:  # an RSS item needs a title or a description
            el.remove(desc)


def apply_content(el, fmt, html_text, head="", tail=""):
    """Put the full text into the entry. head/tail are summary blocks to place before/after it."""
    html_text = "\n".join(part for part in (head, html_text, tail) if part)
    if fmt == "atom":
        content = el.find("{%s}content" % ATOM)
        if content is None:
            content = ET.SubElement(el, "{%s}content" % ATOM)
        for child in list(content):
            content.remove(child)
        content.attrib.pop("src", None)
        content.set("type", "html")
        content.text = html_text
    else:  # RSS: keep <description> exactly as it is, add the full text next to it
        enc = el.find("{%s}encoded" % CONTENT)
        if enc is None:
            enc = ET.SubElement(el, "{%s}encoded" % CONTENT)
        enc.text = html_text


def point_self_link_here(root, fmt, name):
    """A feed's rel=self link names its own address; a reader that trusts it would walk back to the original."""
    holder = root if fmt == "atom" else root.find("channel")
    if holder is None:
        return
    for link in holder.findall("{%s}link" % ATOM):
        if link.get("rel") == "self":
            link.set("href", publisher.public_url(name))


def serialize(root, fmt):
    """XML bytes with the original namespace prefixes. ElementTree's default_namespace option rejects ordinary
    attributes, so the Atom namespace is registered as the default one for the duration of the call instead
    (unless some element has no namespace at all, in which case Atom keeps an `atom:` prefix to stay unambiguous)."""
    ET.register_namespace("content", CONTENT)
    plain = any(isinstance(el.tag, str) and not el.tag.startswith("{") for el in root.iter())
    ET.register_namespace("" if fmt == "atom" and not plain else "atom", ATOM)
    return ET.tostring(root, encoding="utf-8", xml_declaration=True)


# ------------------------------------------------------------------ state
def state_path(name):
    return common.DATA / "state" / "hydrator" / (name[: -len(".feed.xml")] + ".json")


def load_state(name):
    p = state_path(name)
    if p.is_file():
        try:
            st = json.loads(p.read_text(encoding="utf-8"))
            st.setdefault("items", {})
            return st
        except ValueError:
            pass
    return {"etag": None, "last_modified": None, "items": {}}


def save_state(name, st):
    state_path(name).parent.mkdir(parents=True, exist_ok=True)
    atomic_write(state_path(name), json.dumps(st, ensure_ascii=False))


def _signatures(feed):
    """(fetch settings, presentation settings). Changing how articles are fetched invalidates the cache;
    changing only how they are shown just forces the feed to be rewritten."""
    pick = lambda keys: json.dumps({k: feed.get(k) for k in keys}, sort_keys=True)
    return (pick(("selector", "link_from", "min_words", "skip_hosts")),
            pick(("summary", "summary_label", "drop_unhydrated")) + str(VIEW_FORMAT))


def _pending(rec):
    return rec is None or (rec.get("html") is None and rec.get("tries", 0) < MAX_TRIES)


# ------------------------------------------------------------------ fetching articles
def fetch_article(url, feed, cfg):
    """Full text of one article page. Raises Skip for pages that are no articles and anything else on failure."""
    auto = feed["selector"] == AUTO
    host = (urlparse(url).hostname or "").lower()
    for h in feed.get("skip_hosts", DEFAULT_SKIP_HOSTS if auto else []):
        if host == h or host.endswith("." + h):
            raise Skip("%s is on the skip list" % host)
    r = http_get(url, timeout=cfg["timeout"], allow_private=cfg["allow_private_hosts"])
    ctype = r.headers.get("Content-Type")
    if ctype and r.headers.get_content_type() not in ("text/html", "application/xhtml+xml"):
        raise Skip("not a web page (%s)" % r.headers.get_content_type())
    page = decode_html(r.body, ctype or "")
    out = extract_article(page, r.url) if auto else extract_html(page, feed["selector"], r.url)
    if not out.strip():
        raise ValueError("no article text found" if auto else "selector %r matched nothing" % feed["selector"])
    words = len(re.sub(r"<[^>]*>", " ", out).split())
    if words < feed.get("min_words", 60 if auto else 0):
        raise ValueError("only %d words extracted, probably not an article" % words)
    return out


def refresh(feed, cfg):
    """One pass over one feed. Returns the number of articles hydrated."""
    url = feed["url"]
    name = feed_filename(url, feed.get("name"))
    st = load_state(name)
    timeout = cfg["timeout"]
    published = publisher.exists(name)
    drop_mode = feed.get("drop_unhydrated", True)

    fetch_sig, view_sig = _signatures(feed)
    force = (st.get("fetch_sig"), st.get("view_sig")) != (fetch_sig, view_sig)  # new state, upgrade or changed settings
    if st.get("fetch_sig") not in (None, fetch_sig):
        st["items"] = {}  # the selector (or similar) changed: hydrate everything again, with the new settings
    st["fetch_sig"], st["view_sig"] = fetch_sig, view_sig

    cond = {}
    if published and not force and st.get("etag"):
        cond["If-None-Match"] = st["etag"]
    if published and not force and st.get("last_modified"):
        cond["If-Modified-Since"] = st["last_modified"]
    r = http_get(url, cond, timeout)
    if r.status == 304:
        log("%s: not modified" % name)
        return 0

    fmt, keys = scan_keys(r.body)
    st["etag"] = r.headers.get("ETag")
    st["last_modified"] = r.headers.get("Last-Modified")
    pending = [k for k in keys if _pending(st["items"].get(k))]
    if not pending and published and not force:
        save_state(name, st)
        log("%s: nothing new (%d entries)" % (name, len(keys)))
        return 0

    root, fmt = parse_feed(r.body)
    done, drop = 0, []
    for el in entries_of(root, fmt):
        key = entry_key(el, fmt)
        if not key:
            if drop_mode:
                drop.append(el)  # cannot be tracked, so it cannot be hydrated either
            continue
        rec = st["items"].get(key)
        if _pending(rec) and done < cfg["max_items_per_run"]:
            link = article_url(el, fmt, feed.get("link_from", "link"))
            html_text, tries, fetched = None, (rec or {}).get("tries", 0) + 1, False
            if not link:
                log("%s: no article link found for %s" % (name, key))
            elif fmt == "rss" and _text(el.find("comments")) == link:
                tries = MAX_TRIES  # the link is the discussion itself (a text post such as Ask HN): nothing to fetch
            else:
                if done:
                    time.sleep(cfg["request_delay"])  # be polite to the sites
                fetched = True
                try:
                    html_text = fetch_article(link, feed, cfg)
                    tries -= 1
                except Skip as e:
                    log("%s: skipped %s (%s)" % (name, link, e))
                    tries = MAX_TRIES
                except Exception as e:
                    log("%s: %s: %s" % (name, link, e))
            rec = st["items"][key] = {"html": html_text, "tries": tries}
            done += 1 if fetched else 0
        if rec and rec.get("html"):
            # summary: keep   - the text only; the entry's own summary/description stays where it is
            #          start  - the summary moves to the beginning of the content (and leaves its own element)
            #          end    - the same, to the end of the content
            mode = feed.get("summary", "keep")
            summary = original_html(el, fmt) if mode != "keep" else ""
            block = publisher.summary_block(summary, feed.get("summary_label", "Summary"), "after" if mode == "start" else "before")
            apply_content(el, fmt, rec["html"], block if mode == "start" else "", block if mode == "end" else "")
            if block:
                remove_summary(el, fmt)
        elif drop_mode:
            # Not hydrated: skipped for good (text post, PDF, video site), given up after MAX_TRIES, or still
            # waiting its turn. Either way it stays out of the feed; the hydrated ones are all a reader sees.
            drop.append(el)
    holder = root if fmt == "atom" else root.find("channel")
    for el in drop:
        holder.remove(el)
    st["items"] = {k: v for k, v in st["items"].items() if k in set(keys)}  # forget entries that left the feed
    point_self_link_here(root, fmt, name)
    publisher.publish(name, serialize(root, fmt))
    save_state(name, st)  # after publishing, so a crash in between just redoes the work
    log("%s: %d article(s) fetched, %d of %d entries published, %d left out" % (name, done, len(keys) - len(drop), len(keys), len(drop)))
    return done


def run_once(cfg):
    for feed in cfg["feeds"]:
        try:
            refresh(feed, cfg)
        except Exception as e:  # one broken feed must not stop the others
            log("%s failed: %s" % (feed["url"], e))


def make_worker():
    """-> (step, interval_seconds) for the orchestrator."""
    cfg = load_config()
    for f in cfg["feeds"]:
        log("hydrating %s -> %s" % (f["url"], publisher.public_url(feed_filename(f["url"], f.get("name")))))
    return (lambda: run_once(cfg)), float(cfg["poll_minutes"]) * 60

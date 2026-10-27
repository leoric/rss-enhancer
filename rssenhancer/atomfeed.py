"""Feed format helpers shared by the sources: RSS 2.0 -> Atom 1.0 conversion and XML serialisation.

The conversion is based on the rss2atom.py example and keeps its mapping:
  channel title/link/description/language/lastBuildDate -> feed title / link alternate / subtitle / xml:lang / updated
  item <content:encoded> -> <content type="html">, <description> -> <summary>, <pubDate> -> published + updated,
  <author> / <dc:creator> -> author, <category> -> category
and adds what a reader needs to treat the result as a proper feed: a rel="self" link, a stable feed id, entry ids
that are always valid IRIs, <comments> -> rel="replies", <enclosure> -> rel="enclosure", <copyright> -> rights,
<image> -> logo. Elements of other namespaces (rian:, itunes:, ...) are not carried over.

ElementTree keeps its namespace table process-wide, and the sources run in parallel threads, so every
serialisation goes through serialize() under one lock.
"""
import re
import threading
import uuid
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

ATOM = "http://www.w3.org/2005/Atom"
CONTENT = "http://purl.org/rss/1.0/modules/content/"
DC = "http://purl.org/dc/elements/1.1/"
XML_LANG = "{http://www.w3.org/XML/1998/namespace}lang"
NS_LOCK = threading.RLock()


def _a(name):
    return "{%s}%s" % (ATOM, name)


def _text(el, path):
    node = el.find(path)
    return node.text.strip() if node is not None and node.text and node.text.strip() else None


def _date(value):
    """RFC 822 date -> RFC 3339 (what Atom wants); None when it cannot be read."""
    if not value:
        return None
    try:
        dt = parsedate_to_datetime(value.strip())
    except (ValueError, TypeError, OverflowError, IndexError):
        return None
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.isoformat(timespec="seconds")


def _entry_id(guid, link, fallback):
    """An Atom id must be an IRI. A guid or link that already is one is used as it is; anything else becomes a
    stable urn:uuid derived from it, so the same entry keeps its id from one run to the next."""
    for value in (guid, link):
        if value and re.match(r"^[A-Za-z][A-Za-z0-9+.-]*:\S+$", value):
            return value
    return "urn:uuid:%s" % uuid.uuid5(uuid.NAMESPACE_URL, guid or link or fallback)


def _person(text):
    """RSS author: 'mail@host (Name)', 'Name <mail@host>' or just a name -> the name."""
    m = re.fullmatch(r"\s*[^\s<>()]+@[^\s<>()]+\s*\(([^)]+)\)\s*", text)
    if m:
        return m.group(1).strip()
    m = re.fullmatch(r"\s*(.+?)\s*<[^<>\s]+@[^<>\s]+>\s*", text)
    return m.group(1).strip() if m else text.strip()


def _typed(parent, name, text):
    el = ET.SubElement(parent, _a(name), {"type": "html" if re.search(r"[<&]", text) else "text"})
    el.text = text
    return el


def rss_to_atom(rss, self_url=None, description_is_content=False):
    """Convert a parsed RSS 2.0 tree to an Atom <feed> element.

    self_url               where the converted feed is published (becomes rel="self" and the feed id)
    description_is_content the RSS <description> holds the whole text (as in the mail feed) and so becomes
                           <content>; otherwise it is the summary, as in the example script
    """
    if rss.tag != "rss":
        raise ValueError("expected an RSS 2.0 document with a root <rss> element")
    channel = rss.find("channel")
    if channel is None:
        raise ValueError("the RSS document has no <channel>")

    title = _text(channel, "title") or "Untitled feed"
    link = _text(channel, "link")
    feed = ET.Element(_a("feed"))
    lang = _text(channel, "language")
    if lang:
        feed.set(XML_LANG, lang)
    ET.SubElement(feed, _a("title")).text = title
    feed_id = self_url or link or "urn:uuid:%s" % uuid.uuid5(uuid.NAMESPACE_URL, title)
    ET.SubElement(feed, _a("id")).text = feed_id
    updated_el = ET.SubElement(feed, _a("updated"))  # filled in once the entries are known
    if link:
        ET.SubElement(feed, _a("link"), {"rel": "alternate", "href": link})
    if self_url:
        ET.SubElement(feed, _a("link"), {"rel": "self", "type": "application/atom+xml", "href": self_url})
    subtitle = _text(channel, "description")
    if subtitle:
        _typed(feed, "subtitle", subtitle)
    rights = _text(channel, "copyright")
    if rights:
        ET.SubElement(feed, _a("rights")).text = rights
    logo = _text(channel, "image/url")
    if logo:
        ET.SubElement(feed, _a("logo")).text = logo

    newest = None
    for index, item in enumerate(channel.findall("item"), 1):
        entry = ET.SubElement(feed, _a("entry"))
        item_link, guid = _text(item, "link"), _text(item, "guid")
        ET.SubElement(entry, _a("title")).text = _text(item, "title") or "Untitled"
        ET.SubElement(entry, _a("id")).text = _entry_id(guid, item_link, "%s:item:%d" % (feed_id, index))
        if item_link:
            ET.SubElement(entry, _a("link"), {"rel": "alternate", "href": item_link})
        comments = _text(item, "comments")
        if comments:
            ET.SubElement(entry, _a("link"), {"rel": "replies", "type": "text/html", "href": comments})
        for enc in item.findall("enclosure"):
            if enc.get("url"):
                attrs = {"rel": "enclosure", "href": enc.get("url")}
                attrs.update({k: enc.get(k) for k in ("type", "length") if enc.get(k)})
                ET.SubElement(entry, _a("link"), attrs)

        encoded = item.find("{%s}encoded" % CONTENT)
        body = encoded.text if encoded is not None and encoded.text and encoded.text.strip() else None
        desc = item.find("description")
        desc = desc.text if desc is not None and desc.text and desc.text.strip() else None
        if body:
            ET.SubElement(entry, _a("content"), {"type": "html"}).text = body
            if desc:
                _typed(entry, "summary", desc)
        elif desc:
            if description_is_content:
                ET.SubElement(entry, _a("content"), {"type": "html"}).text = desc
            else:
                _typed(entry, "summary", desc)

        author = _text(item, "author") or _text(item, "{%s}creator" % DC)
        if author:
            ET.SubElement(ET.SubElement(entry, _a("author")), _a("name")).text = _person(author)
        for cat in item.findall("category"):
            if cat.text and cat.text.strip():
                ET.SubElement(entry, _a("category"), {"term": cat.text.strip()})
        published = _date(_text(item, "pubDate"))
        if published:
            ET.SubElement(entry, _a("published")).text = published
            newest = max(newest or published, published, key=_sort_key)
        entry_updated = ET.SubElement(entry, _a("updated"))
        entry_updated.text = published or ""  # entries without a date get the feed's date, below

    feed_updated = (_date(_text(channel, "lastBuildDate")) or _date(_text(channel, "pubDate")) or newest
                    or datetime.now(timezone.utc).isoformat(timespec="seconds"))
    updated_el.text = feed_updated
    for el in feed.iter(_a("updated")):
        if not el.text:
            el.text = feed_updated
    return feed


def _sort_key(iso):
    return datetime.fromisoformat(iso).timestamp()


def serialize(root, as_atom, indent=False):
    """XML bytes. The Atom namespace is the default one for Atom documents and gets the `atom:` prefix in RSS ones
    (unless some element has no namespace at all: then Atom keeps a prefix too, to stay unambiguous).
    ElementTree's default_namespace option rejects ordinary attributes, so the registration is changed instead."""
    with NS_LOCK:
        ET.register_namespace("content", CONTENT)
        plain = any(isinstance(el.tag, str) and not el.tag.startswith("{") for el in root.iter())
        ET.register_namespace("" if as_atom and not plain else "atom", ATOM)
        if indent:
            ET.indent(root, space="  ")  # only touches whitespace between elements, never text
        return ET.tostring(root, encoding="utf-8", xml_declaration=True)

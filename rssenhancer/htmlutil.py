"""HTML helpers for the feed hydrator.

decode_html()      bytes of a web page -> text (HTTP header / <meta> charset, then UTF-8)
extract_html()     pick elements with a CSS selector (see cssfilter.py) and return their *content* as clean, well-formed
                   HTML that an RSS reader understands: only a safe set of tags and attributes, absolute URLs,
                   lazy-loaded images resolved, scripts/styles/forms dropped, unclosed tags repaired.
extract_article()  the same for pages nobody wrote a selector for: finds the main text on its own
                   ("reader mode", a heuristic in the spirit of Readability).
clean_fragment()   sanitise an HTML snippet (for example a feed's own description).

Only the standard library is used. The result is valid XML-style markup (void tags are written `<br/>`,
attributes are double-quoted and escaped), so it can be embedded in Atom `<content type="html">` or RSS
`<content:encoded>` without surprises.
"""
import html as _html
import re
from html.parser import HTMLParser
from urllib.parse import urljoin

from .cssfilter import compile_selector, find_all

VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"}

ALLOWED = {
    "a", "abbr", "b", "blockquote", "caption", "cite", "code", "dd", "del", "div", "dl", "dt", "em",
    "figcaption", "figure", "h1", "h2", "h3", "h4", "h5", "h6", "i", "ins", "li", "mark", "ol", "p", "pre",
    "q", "s", "small", "strong", "sub", "sup", "table", "tbody", "td", "tfoot", "th", "thead", "tr", "u", "ul",
}
# dropped together with everything inside them
DROP = {"script", "style", "noscript", "object", "embed", "form", "svg", "button", "input", "select",
        "textarea", "template", "head", "title", "meta", "link", "canvas", "audio", "video", "map", "dialog"}
# containers whose whitespace-only text children are just source formatting
BLOCKISH = {"#root", "div", "ul", "ol", "dl", "table", "thead", "tbody", "tfoot", "tr", "blockquote", "figure", "section", "article"}

_CLOSES_P = {"p", "div", "ul", "ol", "dl", "table", "h1", "h2", "h3", "h4", "h5", "h6", "blockquote", "pre", "hr",
             "figure", "section", "article", "header", "footer", "nav", "aside", "form", "fieldset", "address"}
_P_STOPS = {"div", "td", "th", "li", "blockquote", "table", "figure", "section", "article", "body", "html"}


class Node:
    __slots__ = ("tag", "attrs", "children", "parent")

    def __init__(self, tag, attrs=None, parent=None):
        self.tag = tag
        self.attrs = attrs or {}
        self.children = []  # Node or str
        self.parent = parent


class _Builder(HTMLParser):
    """Forgiving tree builder: repairs unclosed <p>/<li>/<td>..., ignores stray end tags."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.root = Node("#root")
        self.stack = [self.root]

    def _close(self, tag, stops):
        for i in range(len(self.stack) - 1, 0, -1):
            t = self.stack[i].tag
            if t == tag:
                del self.stack[i:]
                return
            if t in stops:
                return

    def handle_starttag(self, tag, attrs):
        if tag in _CLOSES_P:
            self._close("p", _P_STOPS)
        if tag == "li":
            self._close("li", {"ul", "ol"})
        elif tag in ("dt", "dd"):
            self._close("dt", {"dl"})
            self._close("dd", {"dl"})
        elif tag in ("td", "th"):
            self._close("td", {"tr", "table"})
            self._close("th", {"tr", "table"})
        elif tag == "tr":
            self._close("td", {"table"})
            self._close("th", {"table"})
            self._close("tr", {"table"})
        node = Node(tag, {k: (v or "") for k, v in attrs}, self.stack[-1])
        self.stack[-1].children.append(node)
        if tag not in VOID:
            self.stack.append(node)

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if tag not in VOID:
            self.handle_endtag(tag)

    def handle_endtag(self, tag):
        if tag in VOID:
            return
        for i in range(len(self.stack) - 1, 0, -1):
            if self.stack[i].tag == tag:
                del self.stack[i:]
                return

    def handle_data(self, data):
        self.stack[-1].children.append(data)


# ------------------------------------------------------------------ decoding
def decode_html(body, content_type=""):
    """Bytes -> str using the HTTP charset, then a <meta> charset in the first 4 KB, then UTF-8."""
    cands = []
    m = re.search(r"charset=([\w.-]+)", content_type or "", re.I)
    if m:
        cands.append(m.group(1))
    m = re.search(rb"""<meta[^>]+charset=["']?([\w.-]+)""", body[:4096], re.I)
    if m:
        cands.append(m.group(1).decode("ascii", "ignore"))
    cands.append("utf-8")
    for enc in cands:
        try:
            return body.decode(enc)
        except (LookupError, UnicodeDecodeError):
            continue
    return body.decode("utf-8", "replace")


# ------------------------------------------------------------------ cleaning
def _abs(url, base, schemes):
    url = (url or "").strip()
    if not url:
        return ""
    full = urljoin(base, url)
    return full if full.split(":", 1)[0].lower() in schemes else ""


def _has_content(s):
    if re.search(r"<(img|hr)\b", s):
        return True
    return bool(re.sub(r"[\s\u00a0\u200b]+", "", re.sub(r"<[^>]*>", "", s)))


def _img(n, base):
    a = n.attrs
    for k in ("width", "height"):  # drop tracking pixels and spacers
        m = re.match(r"\d+", a.get(k, ""))
        if m and int(m.group()) <= 2:
            return ""
    cands = [a.get("data-src"), a.get("data-original"), a.get("data-lazy-src"), a.get("data-lazy"), a.get("src")]
    srcset = a.get("srcset") or a.get("data-srcset")
    if srcset:
        cands.append(srcset.split(",")[0].strip().split(" ")[0])
    for c in cands:
        if c and not c.strip().lower().startswith("data:"):
            u = _abs(c, base, ("http", "https"))
            if u:
                return '<img src="%s" alt="%s"/>' % (_html.escape(u, quote=True), _html.escape(a.get("alt", ""), quote=True))
    return ""


def _render(n, base, pre, parent):
    if isinstance(n, str):
        if not pre and not n.strip(" \t\r\n") and parent in BLOCKISH:
            return ""
        return _html.escape(n if pre else re.sub(r"[ \t\r\n\f]+", " ", n), quote=False)
    tag = n.tag
    if tag in DROP:
        return ""
    if tag == "img":
        return _img(n, base)
    if tag == "br":
        return "<br/>"
    if tag == "hr":
        return "<hr/>"
    if tag == "iframe":
        src = _abs(n.attrs.get("src"), base, ("http", "https"))
        return '<a href="%s">[embedded content]</a>' % _html.escape(src, quote=True) if src else ""
    inner = "".join(_render(c, base, pre or tag == "pre", tag) for c in n.children)
    if tag not in ALLOWED:
        return inner  # unknown/presentational wrapper (span, font, section, ...): keep its content
    if tag == "a":
        href = _abs(n.attrs.get("href"), base, ("http", "https", "mailto"))
        if not href:
            return inner
        return '<a href="%s">%s</a>' % (_html.escape(href, quote=True), inner) if _has_content(inner) else ""
    attrs = ""
    if tag in ("td", "th"):
        for k in ("colspan", "rowspan"):
            if re.fullmatch(r"\d{1,3}", n.attrs.get(k, "")):
                attrs += ' %s="%s"' % (k, n.attrs[k])
    elif tag == "ol" and re.fullmatch(r"\d{1,5}", n.attrs.get("start", "")):
        attrs = ' start="%s"' % n.attrs["start"]
    if not _has_content(inner):
        return ""
    return "<%s%s>%s</%s>" % (tag, attrs, inner, tag)


def extract_html(page, selector, base_url="", filters=None, host=""):
    """Content of every outermost element matching `selector`, cleaned and joined with newlines.
    `filters` (a cssfilter.FilterSet) cuts blocks out of the page first. Returns "" when nothing matches."""
    sel = compile_selector(selector)
    builder = _Builder()
    builder.feed(page)
    builder.close()
    if filters:
        filters.apply(builder.root, host)
    matched = find_all(builder.root, sel)
    ids = {id(n) for n in matched}
    parts = []
    for node in (n for n in matched if not any(id(a) in ids for a in _ancestors(n))):
        out = "".join(_render(c, base_url, node.tag == "pre", node.tag) for c in node.children).strip()
        if _has_content(out):
            parts.append(out)
    return "\n".join(parts)


def clean_fragment(fragment, base_url=""):
    """Sanitise an HTML snippet with the same rules as extract_html()."""
    builder = _Builder()
    builder.feed(fragment)
    builder.close()
    return "".join(_render(c, base_url, False, "#root") for c in builder.root.children).strip()


# ------------------------------------------------------------------ reader mode
_UNLIKELY = re.compile(r"banner|breadcrumb|combx|comment|community|cover-wrap|disqus|extra|footer|gdpr|header|legends|menu|"
                       r"related|remark|replies|rss|shoutbox|sidebar|skyscraper|social|sponsor|supplemental|ad-break|agegate|"
                       r"pagination|pager|popup|cookie|consent|newsletter|subscribe|share|toolbar|modal|overlay|promo|advert|widget", re.I)
_MAYBE = re.compile(r"article|body|column|content|main|shadow|post|entry|story|markdown|readme", re.I)
_POS = re.compile(r"article|body|content|entry|hentry|h-entry|main|page|post|text|blog|story|markdown|prose|readme", re.I)
_NEG = re.compile(r"hidden|banner|combx|comment|com-|contact|foot|footer|footnote|gdpr|masthead|media|meta|outbrain|promo|related|"
                  r"scroll|share|shoutbox|sidebar|skyscraper|sponsor|shopping|tags|tool|widget|nav|menu|header|advert|cookie|"
                  r"popup|modal|newsletter|subscribe", re.I)
_HIDDEN = re.compile(r"display\s*:\s*none|visibility\s*:\s*hidden", re.I)
_BASE_SCORE = {"div": 5, "article": 8, "main": 5, "pre": 3, "td": 3, "blockquote": 3, "address": -3, "ol": -3, "ul": -3,
               "dl": -3, "dd": -3, "dt": -3, "li": -3, "form": -3, "h1": -5, "h2": -5, "h3": -5, "h4": -5, "h5": -5,
               "h6": -5, "th": -5}
_CLEANABLE = {"div", "section", "ul", "ol", "table"}


def _label(n):
    return (n.attrs.get("class", "") + " " + n.attrs.get("id", "")).strip()


def _is_noise(n):
    if n.tag in DROP or n.tag in ("nav", "aside", "footer"):
        return True
    a = n.attrs
    if "hidden" in a or a.get("aria-hidden") == "true" or _HIDDEN.search(a.get("style", "")):
        return True
    label = _label(n)
    return bool(label) and n.tag not in ("html", "body", "article", "main") and bool(_UNLIKELY.search(label)) and not _MAYBE.search(label)


def _prune(root):
    stack = [root]
    while stack:
        n = stack.pop()
        keep = []
        for c in n.children:
            if isinstance(c, Node):
                if _is_noise(c):
                    continue
                stack.append(c)
            keep.append(c)
        n.children = keep


def _stats(root):
    """id(node) -> (text length, text length inside links, commas), computed bottom-up without recursion."""
    info, stack = {}, [(root, False)]
    while stack:
        n, done = stack.pop()
        if not done:
            stack.append((n, True))
            stack.extend((c, False) for c in reversed(n.children) if isinstance(c, Node))
            continue
        t = l = c = 0
        for ch in n.children:
            if isinstance(ch, str):
                txt = re.sub(r"\s+", " ", ch).strip()
                t += len(txt)
                c += txt.count(",") + txt.count("\uff0c")
            else:
                ct, cl, cc = info[id(ch)]
                t, l, c = t + ct, l + cl, c + cc
        info[id(n)] = (t, t if n.tag == "a" else l, c)
    return info


def _class_weight(n):
    w = 0
    for key in ("class", "id"):
        v = n.attrs.get(key, "")
        if v:
            w -= 25 if _NEG.search(v) else 0
            w += 25 if _POS.search(v) else 0
    return w


def _direct_text(n):
    return sum(len(re.sub(r"\s+", " ", c).strip()) for c in n.children if isinstance(c, str))


def _text_of(n):
    out, stack = [], [n]
    while stack:
        x = stack.pop()
        if isinstance(x, str):
            out.append(x)
        else:
            stack.extend(reversed(x.children))
    return re.sub(r"\s+", " ", "".join(out)).strip()


def _all_nodes(root):
    out, stack = [], [root]
    while stack:
        n = stack.pop()
        out.append(n)
        stack.extend(reversed([c for c in n.children if isinstance(c, Node)]))
    return out


def _pick(root, info, nodes):
    """The node(s) that hold the article text."""
    ld = lambda n: info[id(n)][1] / max(info[id(n)][0], 1)
    # one substantial <article>: that is the page's story (a listing page has many, and is scored instead)
    articles = [n for n in nodes if n.tag == "article" and info[id(n)][0] >= 500 and ld(n) < 0.5]
    top_articles = [a for a in articles if not any(p.tag == "article" for p in _ancestors(a))]
    if len(top_articles) == 1:
        return [top_articles[0]]

    scores = {}
    for n in nodes:
        if n.tag in ("p", "pre", "td") or (n.tag == "div" and _direct_text(n) >= 80):
            t, _, commas = info[id(n)]
            parent = n.parent
            grand = parent.parent if parent is not None else None
            if t < 25 or parent is None or parent.tag in ("#root", "html"):
                continue
            points = 1 + commas + min(t // 100, 3)
            for target, share in ((parent, 1.0), (grand, 0.5)):
                if target is not None and target.tag != "#root":
                    if id(target) not in scores:
                        scores[id(target)] = [_BASE_SCORE.get(target.tag, 0) + _class_weight(target), target]
                    scores[id(target)][0] += points * share
    if not scores:
        for tag in ("article", "main", "body"):
            found = [n for n in nodes if n.tag == tag and info[id(n)][0] > 0]
            if found:
                return [max(found, key=lambda n: info[id(n)][0])]
        return []
    for entry in scores.values():
        entry[0] *= 1 - ld(entry[1])
    top_score, top = max(scores.values(), key=lambda e: e[0])
    if top_score <= 0:
        return []
    chosen, parent = [top], top.parent
    if parent is not None:  # the story is often split over sibling blocks
        threshold = max(10, top_score * 0.2)
        chosen = []
        for sib in parent.children:
            if sib is top:
                chosen.append(sib)
            elif isinstance(sib, Node):
                s = scores.get(id(sib), [0])[0]
                include = s >= threshold
                if not include and sib.tag == "p":
                    t = info[id(sib)][0]
                    include = (t > 80 and ld(sib) < 0.25) or (0 < t <= 80 and ld(sib) == 0 and re.search(r"\.( |$)", _text_of(sib)))
                if include:
                    chosen.append(sib)
    return chosen


def _ancestors(n):
    p = n.parent
    while p is not None:
        yield p
        p = p.parent


def _clean_selected(wrapper, info):
    """Drop link farms and small widget-like blocks that slipped into the chosen content."""
    stack = [wrapper]
    while stack:
        n = stack.pop()
        keep = []
        for c in n.children:
            if isinstance(c, Node) and c.tag in _CLEANABLE:
                t, l, _ = info.get(id(c), (0, 0, 0))
                density = l / max(t, 1)
                if (_class_weight(c) < 0 and t < 300) or (density > 0.5 and t < 600):
                    continue
            if isinstance(c, Node):
                stack.append(c)
            keep.append(c)
        n.children = keep


def extract_article(page, base_url="", filters=None, host=""):
    """Main text of an arbitrary article page as clean HTML ("" if no article-like text is found).
    `filters` (a cssfilter.FilterSet) cuts blocks out of the page first."""
    builder = _Builder()
    builder.feed(page)
    builder.close()
    root = builder.root
    if filters:
        filters.apply(root, host)
    _prune(root)
    info = _stats(root)
    chosen = _pick(root, info, _all_nodes(root))
    if not chosen:
        return ""
    wrapper = Node("div")
    wrapper.children = list(chosen)
    _clean_selected(wrapper, info)
    out = "".join(_render(c, base_url, False, "div") for c in wrapper.children).strip()
    return out if _has_content(out) else ""

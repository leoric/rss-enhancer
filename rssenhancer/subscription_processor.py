"""Subscription processor: turns a plus-addressed mailbox into an AI-split RSS feed.

  1. Poll an IMAP mailbox for mail addressed to EXACTLY foo+bar@baz.com
     (foo@baz.com and foo+other@baz.com are ignored).
  2. Flatten each email into numbered blocks (headings, paragraphs, images).
  3. Ask an AI provider to group the blocks into topics, then (second pass, full text) to write a
     faithful retelling of each topic of at least SUMMARY_LIMIT_WORDS; shorter topics get none.
  4. Hand each topic to the publisher as its own RSS item (summary first, then full text + images).

It knows nothing about other feeds; the feed hydrator is its parallel sibling and they only share the publisher.

Settings (environment or .env):
  SUBSCRIPTION_ADDRESS (required, e.g. foo+bar@baz.com)  IMAP_PASSWORD (required, an app password)
  IMAP_HOST (imap.gmail.com)  IMAP_USER (address without +tag)  IMAP_FOLDER (INBOX)
  SUBSCRIPTION_POLL_MINUTES (15)  SUBSCRIPTION_LOOKBACK_DAYS (7)  SUBSCRIPTION_FEED_TITLE
  SUBSCRIPTION_FEED_FORMAT (rss | atom)
Topic filters, applied right after the email is split and before any summary is considered:
  SUBSCRIPTION_SKIP_KEYWORDS      comma-separated words that drop a topic when its title contains one
  SUBSCRIPTION_SKIP_KEYWORDS_FILE the same, one per line (`!` starts a comment)
  SUBSCRIPTION_MIN_TOPIC_CHARS    topics shorter than this many characters are dropped (0 = off)
Summaries (a retelling, not a gist), sized by two limits:
  SUMMARY_LIMIT_WORDS (500)   topics shorter than this get no summary; topics up to the second
                              limit get a summary of this size
  SUMMARY_LONG_WORDS (1000)   topics longer than this get LIMIT * SUMMARY_FACTOR words (0.5 -> 250)
  SUMMARY_RATIO (off)         optional: also keep the summary under RATIO * topic length, e.g. 0.5
The AI provider is configured in ai.py's docstring.
"""
import datetime as dt
import email
import hashlib
import html
import imaplib
import json
import re
import xml.etree.ElementTree as ET
from pathlib import Path
from email import policy
from email.utils import getaddresses, parseaddr, parsedate_to_datetime
from html.parser import HTMLParser
from urllib.parse import quote

from . import atomfeed, common, publisher
from .ai import extract_json, get_provider, rapid_poll_done
from .common import Worker, atomic_write, env, log

ADDRESS = (env("SUBSCRIPTION_ADDRESS") or "").lower()
IMAP_HOST = env("IMAP_HOST", "imap.gmail.com")
IMAP_USER = env("IMAP_USER") or re.sub(r"\+[^@]*@", "@", ADDRESS)
IMAP_PASSWORD = env("IMAP_PASSWORD")
FOLDER = env("IMAP_FOLDER", "INBOX")
POLL_MINUTES = float(env("SUBSCRIPTION_POLL_MINUTES", "15"))
LOOKBACK_DAYS = int(env("SUBSCRIPTION_LOOKBACK_DAYS", "7"))
FEED_FORMAT = (env("SUBSCRIPTION_FEED_FORMAT", "rss") or "rss").lower()  # rss | atom
MAX_ITEMS = 500
SKIP_RULES = []  # compiled title keywords, loaded by make_worker()
MIN_TOPIC_CHARS = int(env("SUBSCRIPTION_MIN_TOPIC_CHARS", "0"))  # topics shorter than this many characters are dropped; 0 = off
MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
ADDR_HEADERS = ["To", "Cc", "Delivered-To", "X-Original-To", "Envelope-To", "X-Forwarded-To"]


def mask_address(addr):
    """foo+bar@baz.com -> f***+bar@b***.com (keeps only the +tag readable)."""
    local, _, domain = addr.partition("@")
    base, plus, tag = local.partition("+")
    labels = domain.split(".")
    dom = labels[0][:1] + "***" + ("." + labels[-1] if len(labels) > 1 else "")
    return "%s***%s%s@%s" % (base[:1], plus, tag, dom)


def scrub(text):
    """Replace the mailbox address (also URL-encoded) wherever it shows up in the published text."""
    for addr in {ADDRESS, IMAP_USER}:
        if not addr:
            continue
        masked = mask_address(addr)
        for variant in (addr, quote(addr, safe=""), quote(addr, safe="@")):
            text = re.sub(re.escape(variant), masked, text, flags=re.I)
    return text


SYSTEM_PROMPT = """You split emails (usually newsletters) into distinct topics.
You receive the email as numbered blocks like "[12] text" (long blocks are cut off). Blocks starting with "# " are headings; "<image>" is an image.
Return ONLY a JSON object:
{"topics": [{"title": "...", "start": 0, "end": 5}]}
Rules:
- start/end are inclusive block numbers. Topics are in order and must not overlap.
- Each topic is one self-contained story/item/section. If the whole email is a single story, return one topic.
- Include images that belong to a topic inside its range.
- Leave out boilerplate (unsubscribe links, footers, ads, "view in browser", sponsor blurbs) by not covering those blocks.
- title: under 90 characters, descriptive, no clickbait.
No markdown, no commentary, just the JSON."""

SUMMARY_PROMPT = """You condense an article or newsletter section into a faithful retelling.
- Retell what the text says: keep every concrete fact, number, name, date, claim, argument and conclusion, in the original order and in the same language as the text.
- Remove only the water: introductions, repetition, rhetorical flourishes, anecdotes that carry no information, promotions and calls to action.
- Do NOT describe the text ("the article discusses...", "this section covers..."). Write the content itself, as if the author said it in fewer words.
- Add nothing that is not in the text, and do not evaluate it. Do not add an introduction, conclusion or transitions of your own.
- Length: about %(target)d words (the text has about %(words)d). Do not go over %(limit)d words.
- Plain text only: short paragraphs separated by blank lines, no markdown, no headings, no bullet lists unless the original is a list.
Output only the retelling."""


# --------------------------------------------------------------------------- HTML -> blocks
class Flattener(HTMLParser):
    """Flatten messy newsletter HTML into a linear list of clean blocks."""
    BLOCK = {"p", "div", "tr", "table", "ul", "ol", "li", "blockquote", "section", "article",
             "header", "footer", "td", "th", "center", "pre", "hr", "body", "html"}
    HEADINGS = {"h1", "h2", "h3", "h4", "h5", "h6"}
    INLINE = {"a", "b", "strong", "i", "em", "u"}
    SKIP = {"script", "style", "head", "title"}

    def __init__(self, resolve):
        super().__init__(convert_charrefs=True)
        self.resolve = resolve
        self.blocks, self.html, self.text, self.stack = [], [], [], []
        self.skip, self.heading = 0, None

    def flush(self):
        text = "".join(self.text)
        if re.sub(r"[\s\u2022]", "", text):
            inner = "".join(self.html) + "".join("</%s>" % t for t, _ in reversed(self.stack))
            if self.heading:
                self.blocks.append({"kind": "h", "html": "<%s>%s</%s>" % (self.heading, inner, self.heading),
                                    "text": text.strip()})
            else:
                self.blocks.append({"kind": "p", "html": "<p>%s</p>" % inner, "text": text.strip()})
        self.html = [o for _, o in self.stack]  # re-open any inline tags that span the break
        self.text = []

    def handle_starttag(self, tag, attrs):
        if tag in self.SKIP:
            self.skip += 1
            return
        if self.skip:
            return
        d = dict(attrs)
        if tag == "img":
            self.image(d)
        elif tag == "br":
            self.html.append("<br>")
            self.text.append(" ")
        elif tag in self.HEADINGS:
            self.flush()
            self.heading = tag
        elif tag == "li":
            self.flush()
            self.html.append("\u2022 ")
            self.text.append("\u2022 ")
        elif tag in self.BLOCK:
            self.flush()
        elif tag in self.INLINE:
            if tag == "a":
                href = (d.get("href") or "").strip()
                ok = href.lower().startswith(("http://", "https://", "mailto:"))
                opener = '<a href="%s">' % html.escape(href, quote=True) if ok else "<a>"
            else:
                opener = "<%s>" % tag
            self.stack.append((tag, opener))
            self.html.append(opener)

    def handle_endtag(self, tag):
        if tag in self.SKIP:
            self.skip = max(0, self.skip - 1)
            return
        if self.skip:
            return
        if tag in self.HEADINGS:
            self.flush()
            self.heading = None
        elif tag in self.BLOCK:
            self.flush()
        elif tag in self.INLINE:
            for i in range(len(self.stack) - 1, -1, -1):
                if self.stack[i][0] == tag:
                    above = self.stack[i + 1:]
                    self.html.append("".join("</%s>" % t for t, _ in reversed(above)) + "</%s>" % tag
                                     + "".join(o for _, o in above))
                    del self.stack[i]
                    break

    def handle_data(self, data):
        if self.skip:
            return
        data = re.sub(r"\s+", " ", data)
        if data:
            self.html.append(html.escape(data))
            self.text.append(data)

    def image(self, d):
        for k in ("width", "height"):  # drop tracking pixels / spacers
            m = re.match(r"\d+", d.get(k) or "")
            if m and int(m.group()) <= 2:
                return
        url = self.resolve((d.get("src") or "").strip())
        if not url:
            return
        alt = d.get("alt") or ""
        img = '<img src="%s" alt="%s" style="max-width:100%%;height:auto">' % (
            html.escape(url, quote=True), html.escape(alt, quote=True))
        for t, o in reversed(self.stack):  # keep image links clickable
            if t == "a":
                img = o + img + "</a>"
                break
        self.flush()
        self.blocks.append({"kind": "img", "html": "<p>%s</p>" % img, "text": "", "alt": alt})


def plain_blocks(text):
    out = []
    for para in re.split(r"\n\s*\n", text):
        para = para.strip()
        if para:
            h = re.sub(r"(https?://[^\s<]+)", r'<a href="\1">\1</a>', html.escape(para))
            out.append({"kind": "p", "html": "<p>%s</p>" % h.replace("\n", "<br>"), "text": para})
    return out


def build_blocks(msg):
    def resolve(src):
        # Images are hotlinked straight from the email. cid: (attached) images, data: URIs and
        # relative paths have no public URL, so they are dropped.
        return src if src.lower().startswith(("http://", "https://")) else None

    body = msg.get_body(preferencelist=("html", "plain"))
    if body is None:
        return []
    content = body.get_content()
    if body.get_content_type() == "text/html":
        f = Flattener(resolve)
        f.feed(content)
        f.close()
        f.flush()
        return f.blocks
    return plain_blocks(content)


# --------------------------------------------------------------------------- topic splitting
def split_topics(call_ai, subject, sender, blocks):
    lines = []
    for i, b in enumerate(blocks):
        if b["kind"] == "img":
            lines.append('[%d] <image alt="%s">' % (i, b.get("alt", "")[:80]))
        else:
            lines.append("[%d] %s%s" % (i, "# " if b["kind"] == "h" else "", b["text"][:400]))
    prompt = "Subject: %s\nFrom: %s\n\n%s" % (subject, sender, "\n".join(lines))
    raw = call_ai(SYSTEM_PROMPT, prompt)  # API errors propagate -> message is retried next poll
    try:
        topics, last = [], -1
        for t in extract_json(raw)["topics"]:
            s, e = max(int(t["start"]), last + 1), min(int(t["end"]), len(blocks) - 1)
            if e >= s:
                topics.append({"title": str(t["title"]).strip() or subject, "summary": "", "start": s, "end": e})
                last = e
        if topics:
            return topics
    except (ValueError, KeyError, TypeError) as e:
        log("could not parse AI output, publishing whole email:", e)
    return [{"title": subject, "summary": "", "start": 0, "end": len(blocks) - 1}]


SUMMARY_LIMIT_WORDS = int(env("SUMMARY_LIMIT_WORDS", "500"))   # first limit
SUMMARY_LONG_WORDS = int(env("SUMMARY_LONG_WORDS", "1000"))    # second limit
SUMMARY_FACTOR = float(env("SUMMARY_FACTOR", "0.5"))
SUMMARY_RATIO = float(env("SUMMARY_RATIO", "0"))  # optional: also keep the summary under RATIO * topic length


def count_words(text):
    n = len(text.split())
    return n if len(text) / max(n, 1) < 12 else len(text) // 2  # CJK text has no spaces


def summary_target(words):
    """Summary size in words for a topic of `words` words, or None for no summary.
      shorter than the first limit             -> no summary (short enough to read as is)
      first limit .. second limit              -> a summary the size of the first limit
      longer than the second limit             -> first limit * FACTOR (half), to rein in big topics
    SUMMARY_RATIO, if set, additionally caps the summary at that fraction of the topic."""
    if words < SUMMARY_LIMIT_WORDS:
        return None
    target = SUMMARY_LIMIT_WORDS if words <= SUMMARY_LONG_WORDS else round(SUMMARY_LIMIT_WORDS * SUMMARY_FACTOR)
    if SUMMARY_RATIO > 0:
        target = min(target, round(words * SUMMARY_RATIO))
    return max(25, target)


def retell(call_ai, title, text, target):
    """Text -> a retelling of about `target` words; asked once more if the model overshoots badly."""
    words = count_words(text)
    limit = round(target * 1.3) + 5
    system = SUMMARY_PROMPT % {"target": target, "words": words, "limit": limit}
    max_tokens = min(8192, limit * 4 + 2500)  # headroom: reasoning models spend budget on thinking

    def ask(user):
        raw = call_ai(system, user, json_mode=False, max_tokens=max_tokens)
        return re.sub(r"<think>.*?</think>", "", raw or "", flags=re.S).strip()

    base = "Title: %s\n\n%s" % (title, text)
    out = ask(base)
    retried = 0
    if out and count_words(out) > target * 1.5 + 5:
        retried = 1
        second = ask("%s\n\n---\nA first attempt had %d words, which is too long. Write a tighter retelling of "
                     "about %d words (at most %d)." % (base, count_words(out), target, limit))
        if second and count_words(second) < count_words(out):
            out = second
    return out, retried


def summarize_topic(call_ai, title, blocks):
    """A condensed retelling of the full topic text, or "" when the topic is shorter than the limit."""
    text = "\n\n".join(b["text"] for b in blocks if b["kind"] != "img")
    words = count_words(text)
    target = summary_target(words)
    if target is None:
        return ""
    summary, retried = retell(call_ai, title, text, target)
    if not summary:
        log("empty summary for %r (reasoning model out of tokens?)" % title[:50])
    else:
        log("  %s: %d -> %d words (target %d%s)" % (title[:50], words, count_words(summary), target,
                                                    ", retried" if retried else ""))
    return summary


def topic_html(t, blocks, subject, sender):
    parts = []
    if t["summary"]:
        paras = [p.strip() for p in re.split(r"\n\s*\n", t["summary"]) if p.strip()]
        parts.append(publisher.summary_block("".join("<p>%s</p>" % html.escape(p).replace("\n", "<br>") for p in paras)))
    parts += [b["html"] for b in blocks[t["start"]: t["end"] + 1]]
    parts.append("<hr><p><small>From %s \u2014 \u201c%s\u201d</small></p>" % (html.escape(sender), html.escape(subject)))
    return scrub("\n".join(parts))


def item_author(from_header, subject):
    """Author shown in the reader: the sender's display name (the part before <...>), plus " : " and the
    part of the subject before its first colon, e.g.
      Matt Levine <noreply@...> + "Money Stuff: Zuckerberg ..." -> "Matt Levine : Money Stuff"
      Bloomberg Businessweek <noreply@...> + "How the Houthis ..." -> "Bloomberg Businessweek"
    One mailbox can receive several newsletters from the same sender, and the subject prefix tells them apart."""
    name, addr = parseaddr(from_header)
    base = name.strip() or addr
    subj = subject
    while True:  # ignore reply/forward prefixes
        m = re.match(r"\s*(?:re|fwd?|aw|sv|wg)\s*:\s*", subj, re.I)
        if not m:
            break
        subj = subj[m.end():]
    m = re.match(r"(.{1,40}?)\s*:\s", subj)  # a colon followed by whitespace, within the first 40 chars
    tag = m.group(1).strip() if m else ""
    if tag and tag.lower() != base.lower():
        return "%s : %s" % (base, tag) if base else tag
    return base


def compile_keywords(items):
    """Title keywords -> [(keyword as written, compiled regex)]. Matching ignores case.
      sponsor       a whole word (not "sponsorship")
      реклам*       a word that starts with it (реклама, рекламный, ...); * may also stand in the middle or at the start
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
            raise ValueError("bad title keyword %r: %s" % (item, e))
        rules.append((item, rx))
    return rules


def load_skip_rules():
    """Title keywords from SUBSCRIPTION_SKIP_KEYWORDS (comma-separated) and SUBSCRIPTION_SKIP_KEYWORDS_FILE (one per line)."""
    items = (env("SUBSCRIPTION_SKIP_KEYWORDS") or "").split(",")
    name = env("SUBSCRIPTION_SKIP_KEYWORDS_FILE")
    if name:
        path = Path(name).expanduser()
        path = path if path.is_absolute() else common.ROOT / path
        if not path.is_file():
            raise SystemExit("SUBSCRIPTION_SKIP_KEYWORDS_FILE: %s not found" % path)
        items += [ln for ln in path.read_text(encoding="utf-8").splitlines() if not ln.lstrip().startswith("!")]
    try:
        return compile_keywords(items)
    except ValueError as e:
        raise SystemExit("SUBSCRIPTION_SKIP_KEYWORDS: %s" % e)


def keep_topics(topics, blocks):
    """Drop topics whose title contains a skip keyword or whose text is shorter than MIN_TOPIC_CHARS characters
    (the text of the topic without images, as it would be read). These checks come
    before anything that costs an AI call, so a dropped topic is never summarised."""
    kept = []
    for t in topics:
        hit = next((kw for kw, rx in SKIP_RULES if rx.search(t["title"])), None)
        if hit is not None:
            log("  dropped %r: the title contains %r" % (t["title"][:60], hit))
            continue
        if MIN_TOPIC_CHARS > 0:
            chars = sum(len(b["text"]) for b in blocks[t["start"]: t["end"] + 1] if b["kind"] != "img")
            if chars < MIN_TOPIC_CHARS:
                log("  dropped %r: %d characters, fewer than %d" % (t["title"][:60], chars, MIN_TOPIC_CHARS))
                continue
        kept.append(t)
    return kept


def process_message(raw, call_ai):
    msg = email.message_from_bytes(raw, policy=policy.default)
    subject = str(msg["subject"] or "(no subject)").strip()
    sender = str(msg["from"] or "")
    mid = str(msg["message-id"] or hashlib.sha1(raw).hexdigest()).strip()
    try:
        when = parsedate_to_datetime(str(msg["date"]))
        if when.tzinfo is None:
            when = when.replace(tzinfo=dt.timezone.utc)
    except Exception:
        when = dt.datetime.now(dt.timezone.utc)

    blocks = build_blocks(msg)
    if not blocks:
        return []
    topics = split_topics(call_ai, subject, sender, blocks)
    for i, t in enumerate(topics):
        t["index"] = i  # the position in the email, kept in the item id even when topics are dropped
    topics = keep_topics(topics, blocks)
    for t in topics:
        t["summary"] = summarize_topic(call_ai, t["title"], blocks[t["start"]: t["end"] + 1])
    items = []
    for i, t in enumerate(topics):
        items.append({
            "guid": "%s#%d" % (mid, t["index"]),
            "title": t["title"],
            "author": item_author(sender, subject),
            "html": topic_html(t, blocks, subject, sender),
            # a few seconds apart so readers sorting newest-first keep the email's topic order
            "ts": when.timestamp() + (len(topics) - i),
        })
    return items


# --------------------------------------------------------------------------- IMAP
def matching_uids(M, uids, target, sample=None):
    """Exact-address match on To/Cc/Delivered-To/etc. (server-side TO search is only a substring match)."""
    hits = []
    fields = "(BODY.PEEK[HEADER.FIELDS (%s)])" % " ".join(h.upper() for h in ADDR_HEADERS)

    def check(uid, hdr):
        m = email.message_from_bytes(hdr)
        vals = [str(v) for h in ADDR_HEADERS for v in m.get_all(h, [])]
        addrs = {a.lower() for _, a in getaddresses(vals)}
        if sample is not None and len(sample) < 8:
            sample.append(", ".join(sorted(addrs)) or "(no recipients)")
        if target in addrs:
            hits.append(uid)

    for i in range(0, len(uids), 100):
        typ, data = M.uid("fetch", ",".join(map(str, uids[i:i + 100])), fields)
        if typ != "OK":
            continue
        pending = None
        for item in data:
            if isinstance(item, tuple):
                m = re.search(rb"UID (\d+)", item[0])
                if m:
                    check(int(m.group(1)), item[1])
                    pending = None
                else:
                    pending = item[1]
            elif isinstance(item, bytes) and pending is not None:
                m = re.search(rb"UID (\d+)", item)
                if m:
                    check(int(m.group(1)), pending)
                    pending = None
    return sorted(hits)


def poll(state, call_ai):
    M = imaplib.IMAP4_SSL(IMAP_HOST)
    try:
        M.login(IMAP_USER, IMAP_PASSWORD)
        typ, sel = M.select('"%s"' % FOLDER, readonly=True)
        if typ != "OK":
            raise RuntimeError("cannot open folder %r" % FOLDER)
        log("logged in as %s, folder %r has %s message(s)" % (IMAP_USER, FOLDER, sel[0].decode()))
        uv = M.response("UIDVALIDITY")[1][0].decode()
        if state.get("uidvalidity") != uv:
            state["uidvalidity"], state["seen"] = uv, []
        seen = set(state["seen"])

        d = dt.date.today() - dt.timedelta(days=LOOKBACK_DAYS)
        since = "%02d-%s-%d" % (d.day, MONTHS[d.month - 1], d.year)
        typ, data = M.uid("search", None, "SINCE", since)
        found = data[0].split()
        uids = [int(u) for u in found if int(u) not in seen]
        log("%d message(s) since %s, %d not checked yet" % (len(found), since, len(uids)))
        if not found:
            log("nothing in the last %d days; check IMAP_FOLDER (Gmail: '[Gmail]/All Mail' if a filter "
                "skips the inbox) or raise SUBSCRIPTION_LOOKBACK_DAYS" % LOOKBACK_DAYS)
        if not uids:
            return
        sample = []
        hits = matching_uids(M, uids, ADDRESS, sample)
        seen.update(u for u in uids if u not in set(hits))  # never re-check non-matching mail
        log("%d of %d new message(s) addressed to %s" % (len(hits), len(uids), ADDRESS))
        if not hits:
            log("no match. Recipients on the first %d message(s): %s" % (len(sample), " | ".join(sample)))

        for uid in hits:
            try:
                typ, data = M.uid("fetch", str(uid), "(BODY.PEEK[])")
                raw = next(x[1] for x in data if isinstance(x, tuple))
                new = process_message(raw, call_ai)
            except Exception as e:  # leave unseen so it is retried next poll
                log("uid %d failed, will retry: %s" % (uid, e))
                continue
            known = {it["guid"] for it in state["items"]}
            state["items"] = sorted([i for i in new if i["guid"] not in known] + state["items"],
                                    key=lambda i: -i["ts"])[:MAX_ITEMS]
            seen.add(uid)
            state["seen"] = sorted(seen)[-5000:]
            save_state(state)
            publish_items(state["items"])
            log("uid %d -> %d item(s)" % (uid, len(new)))
        state["seen"] = sorted(seen)[-5000:]
    finally:
        try:
            M.logout()
        except Exception:
            pass


# --------------------------------------------------------------------------- state, publishing, worker
def state_file():
    return common.DATA / "state" / "subscriptions.json"


def load_state():
    p = state_file()
    if p.exists():
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except ValueError:
            pass
    return {"uidvalidity": None, "seen": [], "items": []}


def save_state(state):
    state_file().parent.mkdir(parents=True, exist_ok=True)
    atomic_write(state_file(), json.dumps(state))


def publish_items(items):
    title = env("SUBSCRIPTION_FEED_TITLE") or "Mail: " + mask_address(ADDRESS)
    document = publisher.rss_document(
        title, "AI-split topics from mail to " + mask_address(ADDRESS), items, publisher.MAIL_FEED)
    if FEED_FORMAT == "atom":
        # the item's description holds the whole text here, so it becomes the Atom <content>
        feed = atomfeed.rss_to_atom(ET.fromstring(document), publisher.public_url(publisher.MAIL_FEED), description_is_content=True)
        publisher.publish(publisher.MAIL_FEED, atomfeed.serialize(feed, True, indent=True))
    else:
        publisher.publish(publisher.MAIL_FEED, document)


def configured():
    return bool(ADDRESS)


def validate():
    """Check the settings and raise SystemExit with a clear message when they cannot be used. Writes nothing."""
    if "+" not in ADDRESS.split("@")[0]:
        raise SystemExit("Set SUBSCRIPTION_ADDRESS to a plus address, e.g. foo+bar@baz.com")
    if not IMAP_PASSWORD:
        raise SystemExit("Set IMAP_PASSWORD (use an app password)")
    if FEED_FORMAT not in ("rss", "atom"):
        raise SystemExit("SUBSCRIPTION_FEED_FORMAT must be rss or atom")
    if env("SUBSCRIPTION_MIN_TOPIC_WORDS"):
        raise SystemExit("SUBSCRIPTION_MIN_TOPIC_WORDS was replaced by SUBSCRIPTION_MIN_TOPIC_CHARS (a number of characters)")
    load_skip_rules()
    get_provider()


def make_worker():
    """-> a Worker for the orchestrator."""
    validate()
    global SKIP_RULES
    SKIP_RULES = load_skip_rules()
    name, call_ai = get_provider()
    state = load_state()
    publish_items(state["items"])
    log("provider=%s  address=%s  feed=%s" % (name, mask_address(ADDRESS), publisher.public_url(publisher.MAIL_FEED)))

    def step():
        global SKIP_RULES
        try:
            SKIP_RULES = load_skip_rules()  # the keyword file may have been edited since the last pass
        except SystemExit as e:
            log("the keyword list was not reloaded, keeping the previous one: %s" % e)
        try:
            poll(state, call_ai)
            save_state(state)
            log("poll finished, %d item(s) in the feed" % len(state["items"]))
        finally:
            rapid_poll_done()

    return Worker(step, lambda: POLL_MINUTES * 60)

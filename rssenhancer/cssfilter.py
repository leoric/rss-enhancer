"""CSS selectors and uBlock-Origin-style cosmetic filters over the parsed page tree.

A small selector engine written for htmlutil's tree (nodes with .tag, .attrs, .children, .parent; text children are
plain strings). It serves two purposes:
  * a feed's "selector" (which elements hold the article text) accepts the CSS below, not just tag.class
  * "remove" filters cut blocks out of the page before the text is extracted, in uBlock Origin's syntax:
        .article__block:has(div[data-type*="banner"])
        inosmi.ru##.article__block>strong>a
        ~github.com##.newsletter:has-text(/subscribe/i)
        example.org#@#.article__ad            (an exception: cancels a filter with the same selector)

Supported selectors
  type  *  .class  #id  [attr]  [attr=v]  [attr~=v]  [attr|=v]  [attr^=v]  [attr$=v]  [attr*=v]  (flag i / s)
  combinators: descendant (space)  >  +  ~            lists: a, b
  :not()  :is()  :where()  :matches()  :has() (also relative: :has(> img), :has(+ p))
  :first-child  :last-child  :only-child  :nth-child()  :nth-last-child()  :first-of-type  :last-of-type
  :only-of-type  :nth-of-type()  :nth-last-of-type()  :empty
uBlock Origin procedural operators
  :has-text(text | /regex/flags)  (alias :contains())   the element's text matches; plain text is case-sensitive
  :matches-attr("name"="value")   an attribute whose name and value match (plain = exact, or /regex/flags)
  :min-text-length(n)             at least n characters of text
  :upward(n | selector)           continue with the n-th ancestor / the nearest ancestor matching selector (last step only)
  :nth-ancestor(n)                same as :upward(n)
  :remove()                       accepted and ignored: matched elements are always removed
Not supported (a clear error, because they need a live browser): :xpath(), :matches-css*(), :style(), :watch-attr(),
:others(), network filters, scriptlets (##+js) and HTML filters (##^).
"""
import re


class SelectorError(ValueError):
    pass


_IDENT = re.compile(r"(?:[-_a-zA-Z0-9\u00a0-\uffff]|\\.)+")
_UNESCAPE = re.compile(r"\\(.)")
_EXPENSIVE = {"has", "has-text", "contains", "min-text-length", "matches-attr"}
_LOGICAL = {"not", "is", "where", "matches", "any"}
_NTH = {"nth-child", "nth-last-child", "nth-of-type", "nth-last-of-type"}
_PLAIN = {"first-child", "last-child", "only-child", "first-of-type", "last-of-type", "only-of-type", "empty"}
_ACTIONS = {"upward", "nth-ancestor", "remove"}
_UNSUPPORTED = {
    "xpath": "XPath is not supported",
    "matches-css": "needs a rendered page, which is not available here", "matches-css-before": "needs a rendered page",
    "matches-css-after": "needs a rendered page", "matches-media": "needs a browser", "matches-path": "does not apply here",
    "style": "styling is meaningless here; matched elements are removed", "watch-attr": "needs a live page",
    "remove-attr": "not supported", "remove-class": "not supported", "others": "not supported",
    "matches-property": "needs a live page", "-abp-has": "use :has()", "-abp-contains": "use :has-text()",
}


# ------------------------------------------------------------------ parsing
class _Parser:
    def __init__(self, text):
        self.s, self.i = text, 0

    def error(self, msg):
        raise SelectorError("%s (at position %d of %r)" % (msg, self.i, self.s))

    def peek(self):
        return self.s[self.i] if self.i < len(self.s) else ""

    def ws(self):
        start = self.i
        while self.i < len(self.s) and self.s[self.i] in " \t\r\n\f":
            self.i += 1
        return self.i > start

    def ident(self):
        m = _IDENT.match(self.s, self.i)
        if not m:
            self.error("expected a name")
        self.i = m.end()
        return _UNESCAPE.sub(r"\1", m.group())

    def parse_list(self, relative=False):
        items = [self.parse_complex(relative)]
        while True:
            self.ws()
            if self.peek() == ",":
                self.i += 1
                items.append(self.parse_complex(relative))
            else:
                return items

    def parse_complex(self, relative):
        self.ws()
        comb = None
        if relative:
            comb = " "
            if self.peek() in tuple(">+~"):
                comb = self.peek()
                self.i += 1
                self.ws()
        steps = [(comb, self.parse_compound())]
        while True:
            had_space = self.ws()
            c = self.peek()
            if c in ("", ",", ")"):
                return steps
            if c in tuple(">+~"):
                comb = c
                self.i += 1
                self.ws()
            elif had_space:
                comb = " "
            else:
                self.error("unexpected %r" % c)
            steps.append((comb, self.parse_compound()))

    def parse_compound(self):
        comp = {"tag": None, "id": None, "classes": [], "attrs": [], "pseudos": []}
        start = self.i
        c = self.peek()
        if c == "*":
            self.i += 1
        elif c and (c.isalnum() or c in "_\\" or ord(c) > 127):
            comp["tag"] = self.ident().lower()
        while True:
            c = self.peek()
            if c == ".":
                self.i += 1
                comp["classes"].append(self.ident())
            elif c == "#":
                self.i += 1
                comp["id"] = self.ident()
            elif c == "[":
                comp["attrs"].append(self.parse_attr())
            elif c == ":":
                comp["pseudos"].append(self.parse_pseudo())
            else:
                break
        if self.i == start:
            self.error("expected a selector")
        return comp

    def parse_attr(self):
        self.i += 1  # [
        self.ws()
        name = self.ident().lower()
        self.ws()
        op = val = None
        ci = False
        m = re.compile(r"[~|^$*]?=").match(self.s, self.i)
        if m:
            op, self.i = m.group(), m.end()
            self.ws()
            q = self.peek()
            if q in ("'", '"'):
                self.i += 1
                buf = []
                while self.i < len(self.s) and self.s[self.i] != q:
                    if self.s[self.i] == "\\" and self.i + 1 < len(self.s):
                        self.i += 1
                    buf.append(self.s[self.i])
                    self.i += 1
                if self.peek() != q:
                    self.error("unterminated string")
                self.i += 1
                val = "".join(buf)
            else:
                m2 = re.compile(r"[^\s\]]+").match(self.s, self.i)
                if not m2:
                    self.error("expected an attribute value")
                val, self.i = _UNESCAPE.sub(r"\1", m2.group()), m2.end()
            self.ws()
            flag = re.compile(r"[iIsS](?=\s*\])").match(self.s, self.i)
            if flag:
                ci = flag.group().lower() == "i"
                self.i = flag.end()
                self.ws()
        if self.peek() != "]":
            self.error("expected ]")
        self.i += 1
        return name, op, val, ci

    def raw_arg(self):
        """Text up to the matching ) (the opening one is already consumed). Parentheses must balance, or be escaped
        with a backslash, or sit inside a quoted string. A quote opens a string only at the start of a word, so an
        apostrophe inside plain text (:has-text(Don't miss)) is just a character."""
        start, depth, quote, prev = self.i, 1, None, "("
        while self.i < len(self.s):
            ch = self.s[self.i]
            if ch == "\\":
                self.i += 2
                prev = "x"
                continue
            if quote:
                if ch == quote:
                    quote = None
            elif ch in "'\"" and prev in " \t=,(":
                quote = ch
            elif ch == "(":
                depth += 1
            elif ch == ")":
                depth -= 1
                if depth == 0:
                    arg = self.s[start:self.i]
                    self.i += 1
                    return arg
            prev = ch
            self.i += 1
        self.error("missing )")

    def parse_pseudo(self):
        self.i += 1  # :
        if self.peek() == ":":
            self.error("pseudo-elements are not supported")
        name = self.ident().lower()
        has_arg = self.peek() == "("
        if has_arg:
            self.i += 1
        if name in _UNSUPPORTED:
            self.error(":%s() %s" % (name, _UNSUPPORTED[name]))
        if name in _PLAIN:
            if has_arg:
                self.error(":%s takes no argument" % name)
            return name, None
        if not has_arg and name != "remove":
            self.error(":%s needs an argument" % name)
        if name in _LOGICAL or name == "has":
            arg = self.parse_list(relative=(name == "has"))
            self.ws()
            if self.peek() != ")":
                self.error("expected )")
            self.i += 1
            _reject_actions(arg)
            return name, arg
        raw = self.raw_arg() if has_arg else ""
        if name in _NTH:
            return name, _parse_nth(raw)
        if name in ("has-text", "contains"):
            return "has-text", _text_pattern(raw, exact=False)
        if name == "min-text-length":
            if not re.fullmatch(r"\s*\d+\s*", raw):
                self.error(":min-text-length() needs a number")
            return name, int(raw)
        if name == "matches-attr":
            return name, _parse_matches_attr(raw, self)
        if name in ("upward", "nth-ancestor"):
            raw = raw.strip()
            if re.fullmatch(r"\d+", raw):
                n = int(raw)
                if not 1 <= n <= 255:
                    self.error(":%s() takes a number from 1 to 255" % name)
                return "upward", n
            if name == "nth-ancestor":
                self.error(":nth-ancestor() takes a number")
            sub = _Parser(raw)
            sels = sub.parse_list()
            if sub.i != len(raw):
                sub.error("unexpected text")
            _reject_actions(sels)
            return "upward", sels
        if name == "remove":
            return "remove", None
        self.error("unsupported pseudo-class :%s" % name)


def _reject_actions(complexes):
    for steps in complexes:
        for _, comp in steps:
            for kind, _arg in comp["pseudos"]:
                if kind in ("upward", "remove"):
                    raise SelectorError(":%s() can only be the last step of a selector" % kind)


def _parse_nth(arg):
    a = re.sub(r"\s+", "", arg.lower())
    if a == "odd":
        return 2, 1
    if a == "even":
        return 2, 0
    m = re.fullmatch(r"([+-]?\d*)n([+-]\d+)?", a)
    if m:
        k = m.group(1)
        return (-1 if k == "-" else 1 if k in ("", "+") else int(k)), int(m.group(2) or 0)
    if re.fullmatch(r"[+-]?\d+", a):
        return 0, int(a)
    raise SelectorError("cannot read %r as an(n+b)" % arg)


def _nth_ok(index1, ab):
    a, b = ab
    if a == 0:
        return index1 == b
    n, rem = divmod(index1 - b, a)
    return rem == 0 and n >= 0


def _pattern(token, exact):
    """'text', "text", /regex/flags, "/regex/flags" or bare text -> compiled regex
    (plain text: a substring, or the whole value if exact)."""
    token = token.strip()
    if len(token) >= 2 and token[0] == token[-1] and token[0] in "'\"":
        token = token[1:-1]
    m = re.fullmatch(r"/((?:\\.|[^/\\])+)/([a-z]*)", token, re.S)
    if m:
        flags = (re.I if "i" in m.group(2) else 0) | (re.S if "s" in m.group(2) else 0) | (re.M if "m" in m.group(2) else 0)
        try:
            return re.compile(m.group(1), flags)
        except re.error as e:
            raise SelectorError("bad regular expression %s: %s" % (token, e))
    token = re.sub(r"\\(.)", r"\1", token)
    return re.compile("^%s$" % re.escape(token) if exact else re.escape(token))


def _text_pattern(raw, exact):
    if not raw.strip():
        raise SelectorError(":has-text() needs some text")
    return _pattern(raw, exact)


def _parse_matches_attr(raw, parser):
    m = re.fullmatch(r"\s*(\"[^\"]*\"|'[^']*'|/(?:\\.|[^/\\])+/[a-z]*|[^=\s]+)\s*(?:=\s*(.*?))?\s*", raw, re.S)
    if not m:
        parser.error(":matches-attr() expects \"name\"=\"value\"")
    return _pattern(m.group(1), True), (_pattern(m.group(2), True) if m.group(2) is not None else None)


# ------------------------------------------------------------------ matching
class Context:
    """Caches for one pass over one page: text of elements and sibling lists."""

    def __init__(self):
        self._text, self._sib = {}, {}

    def text(self, n):
        t = self._text.get(id(n))
        if t is None:
            out, stack = [], [n]
            while stack:
                x = stack.pop()
                if isinstance(x, str):
                    out.append(x)
                else:
                    stack.extend(reversed(x.children))
            t = self._text[id(n)] = re.sub(r"\s+", " ", "".join(out)).strip()
        return t

    def siblings(self, n):
        p = n.parent
        if p is None:
            return [n], 0
        entry = self._sib.get(id(p))
        if entry is None:
            els = [c for c in p.children if not isinstance(c, str)]
            entry = self._sib[id(p)] = (els, {id(e): i for i, e in enumerate(els)})
        return entry[0], entry[1].get(id(n), 0)


def _children(n):
    return [c for c in n.children if not isinstance(c, str)]


def _descendants(n):
    stack = list(reversed(_children(n)))
    while stack:
        x = stack.pop()
        yield x
        stack.extend(reversed(_children(x)))


def _related(n, comb, ctx):
    if comb == " ":
        return _descendants(n)
    if comb == ">":
        return _children(n)
    els, i = ctx.siblings(n)
    return els[i + 1:i + 2] if comb == "+" else els[i + 1:]


def _attr_ok(attrs, name, op, val, ci):
    if name not in attrs:
        return False
    if op is None:
        return True
    v = attrs[name]
    if ci:
        v, val = v.lower(), val.lower()
    if op == "=":
        return v == val
    if op == "~=":
        return bool(val) and val in v.split()
    if op == "|=":
        return v == val or v.startswith(val + "-")
    if not val:
        return False
    return v.startswith(val) if op == "^=" else v.endswith(val) if op == "$=" else val in v


def _match_compound(n, comp, ctx):
    if n.tag == "#root":
        return False
    if comp["tag"] and n.tag != comp["tag"]:
        return False
    a = n.attrs
    if comp["id"] is not None and a.get("id") != comp["id"]:
        return False
    if comp["classes"]:
        have = a.get("class", "").split()
        if any(c not in have for c in comp["classes"]):
            return False
    for name, op, val, ci in comp["attrs"]:
        if not _attr_ok(a, name, op, val, ci):
            return False
    pseudos = comp["pseudos"]
    for expensive in (False, True):  # cheap structural tests first, text and :has() last
        for kind, arg in pseudos:
            if (kind in _EXPENSIVE) == expensive and not _pseudo(n, kind, arg, ctx):
                return False
    return True


def _pseudo(n, kind, arg, ctx):
    if kind == "not":
        return not any(_match_steps(n, steps, len(steps) - 1, ctx) for steps in arg)
    if kind in _LOGICAL:
        return any(_match_steps(n, steps, len(steps) - 1, ctx) for steps in arg)
    if kind == "has":
        return any(_has(n, steps, ctx) for steps in arg)
    if kind == "has-text":
        return bool(arg.search(ctx.text(n)))
    if kind == "min-text-length":
        return len(ctx.text(n)) >= arg
    if kind == "matches-attr":
        name_rx, value_rx = arg
        return any(name_rx.search(k) and (value_rx is None or value_rx.search(v)) for k, v in n.attrs.items())
    if kind == "remove":
        return True
    els, i = ctx.siblings(n)
    if kind == "first-child":
        return i == 0
    if kind == "last-child":
        return i == len(els) - 1
    if kind == "only-child":
        return len(els) == 1
    if kind == "empty":
        return not _children(n) and not any(c.strip() for c in n.children)
    if kind == "nth-child":
        return _nth_ok(i + 1, arg)
    if kind == "nth-last-child":
        return _nth_ok(len(els) - i, arg)
    same = [e for e in els if e.tag == n.tag]
    j = next(k for k, e in enumerate(same) if e is n)
    if kind in ("first-of-type", "last-of-type", "only-of-type"):
        return j == 0 if kind == "first-of-type" else j == len(same) - 1 if kind == "last-of-type" else len(same) == 1
    if kind == "nth-of-type":
        return _nth_ok(j + 1, arg)
    if kind == "nth-last-of-type":
        return _nth_ok(len(same) - j, arg)
    raise SelectorError("unsupported pseudo-class :%s" % kind)


def _has(anchor, steps, ctx):
    current = [anchor]
    for comb, comp in steps:
        nxt, seen = [], set()
        for c in current:
            for cand in _related(c, comb, ctx):
                if id(cand) not in seen and _match_compound(cand, comp, ctx):
                    seen.add(id(cand))
                    nxt.append(cand)
        if not nxt:
            return False
        current = nxt
    return True


def _match_steps(n, steps, k, ctx):
    comb, comp = steps[k]
    if not _match_compound(n, comp, ctx):
        return False
    if k == 0:
        return True
    if comb == ">":
        return n.parent is not None and _match_steps(n.parent, steps, k - 1, ctx)
    if comb == " ":
        a = n.parent
        while a is not None:
            if _match_steps(a, steps, k - 1, ctx):
                return True
            a = a.parent
        return False
    els, i = ctx.siblings(n)
    if comb == "+":
        return i > 0 and _match_steps(els[i - 1], steps, k - 1, ctx)
    return any(_match_steps(e, steps, k - 1, ctx) for e in els[:i])


# ------------------------------------------------------------------ public API
class Selector:
    def __init__(self, text, complexes):
        self.text = text
        self.complexes = complexes  # [(steps, actions)]

    def __str__(self):
        return self.text


def compile_selector(text):
    """Parse a selector list (with the operators above). Raises SelectorError."""
    if isinstance(text, Selector):
        return text
    p = _Parser(text)
    p.ws()
    lists = p.parse_list()
    p.ws()
    if p.i != len(text):
        p.error("unexpected %r" % p.peek())
    complexes = []
    for steps in lists:
        pseudos = steps[-1][1]["pseudos"]
        actions = []
        while pseudos and pseudos[-1][0] in _ACTIONS:
            actions.insert(0, pseudos.pop())
        _reject_actions([steps])
        complexes.append((steps, actions))
    return Selector(text.strip(), complexes)


def parse_selector(text):
    return compile_selector(text)


def all_elements(root):
    """Every element below root in document order."""
    out, stack = [], list(reversed(_children(root)))
    while stack:
        x = stack.pop()
        out.append(x)
        stack.extend(reversed(_children(x)))
    return out


def _upward(n, action):
    if isinstance(action, int):
        for _ in range(action):
            n = n.parent
            if n is None or n.tag == "#root":
                return None
        return n
    ctx = Context()
    a = n.parent
    while a is not None and a.tag != "#root":
        if any(_match_steps(a, steps, len(steps) - 1, ctx) for steps in action):
            return a
        a = a.parent
    return None


def find_all(root, selector, ctx=None, elements=None):
    """Elements matched by the selector (after any :upward() step), in document order."""
    selector = compile_selector(selector)
    ctx = ctx or Context()
    elements = all_elements(root) if elements is None else elements
    found = {}
    for n in elements:
        for steps, actions in selector.complexes:
            if _match_steps(n, steps, len(steps) - 1, ctx):
                target = n
                for kind, arg in actions:
                    if kind == "upward" and target is not None:
                        target = _upward(target, arg)
                if target is not None:
                    found[id(target)] = target
                break
    order = {id(e): i for i, e in enumerate(elements)}
    return sorted(found.values(), key=lambda e: order.get(id(e), 0))


def _domain_ok(pos, neg, host):
    hit = lambda d: host == d or host.endswith("." + d)
    if any(hit(d) for d in neg):
        return False
    return not pos or any(hit(d) for d in pos)


_SEP = re.compile(r"^(?P<domains>[^#]*?)(?P<sep>#@#|#\?#|##)(?P<sel>.+)$")
_OTHER_SEP = re.compile(r"^[^#]*#@?[$%]#")


class FilterSet:
    """uBlock Origin cosmetic filters: [domains]##selector, [domains]#@#selector (exception), ! comments.
    A bare selector (no ##) applies everywhere. Domains: a.com,b.com,~c.com (subdomains included)."""

    def __init__(self):
        self.rules, self.exceptions, self.lines = [], [], []

    def __bool__(self):
        return bool(self.rules)

    def signature(self):
        return "\n".join(self.lines)

    def add_text(self, text, source="", bare_ok=True):
        for number, line in enumerate(text.splitlines(), 1):
            self.add_line(line, "%s:%d" % (source, number) if source else "", bare_ok)

    def add_line(self, line, where="", bare_ok=True):
        line = line.strip()
        if not line or line.startswith("!") or line.startswith("[Adblock"):
            return
        try:
            if _OTHER_SEP.match(line):
                raise SelectorError("scriptlet and snippet filters are not supported")
            m = _SEP.match(line)
            if not m:
                if not bare_ok:
                    raise SelectorError("not a cosmetic filter (expected domains##selector)")
                domains, sep, sel = "", "##", line
            else:
                domains, sep, sel = m.group("domains"), m.group("sep"), m.group("sel").strip()
            if sel.startswith("+js(") or sel.startswith("^"):
                raise SelectorError("scriptlet and HTML filters are not supported")
            pos, neg = [], []
            for d in (x.strip().lower() for x in domains.split(",")):
                if not d:
                    continue
                if "*" in d or d.lstrip("~").startswith("/"):
                    raise SelectorError("wildcard and regular-expression domains are not supported: %s" % d)
                (neg if d.startswith("~") else pos).append(d.lstrip("~"))
            if sep == "#@#":
                self.exceptions.append((pos, neg, sel))
            else:
                self.rules.append((pos, neg, sel, compile_selector(sel)))
        except SelectorError as e:
            raise SelectorError("%s%s: %s" % (where + ": " if where else "", line, e))
        self.lines.append(line)

    def apply(self, root, host=""):
        """Remove every element the applicable filters match. All filters are evaluated on the page as it was
        before any removal, so their order does not matter. Returns the number of elements removed."""
        host = (host or "").lower()
        active = [r for r in self.rules if _domain_ok(r[0], r[1], host)]
        blocked = {sel for pos, neg, sel in self.exceptions if _domain_ok(pos, neg, host)}
        active = [r for r in active if r[2] not in blocked]
        if not active:
            return 0
        ctx, elements, targets = Context(), all_elements(root), {}
        for _pos, _neg, _sel, selector in active:
            for n in find_all(root, selector, ctx, elements):
                targets[id(n)] = n
        by_parent = {}
        for n in targets.values():
            if n.parent is not None:
                by_parent.setdefault(id(n.parent), (n.parent, set()))[1].add(id(n))
        for parent, ids in by_parent.values():
            parent.children = [c for c in parent.children if isinstance(c, str) or id(c) not in ids]
        return len(targets)

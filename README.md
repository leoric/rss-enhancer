# RssEnhancer

Turns feeds into something worth reading, and serves them as local RSS. Pure Python standard library, built for macOS
(works anywhere with Python 3.12+).

```
                 +-----------------------+
 IMAP mailbox -> | subscription processor| --\
                 +-----------------------+     \     +-----------+      http://127.0.0.1:8787/
                      (parallel, independent)    +-> | publisher | ---> <name>.feed.xml
                 +-----------------------+     /     +-----------+
 third-party  -> |    feed hydrator      | --/
 RSS / Atom      +-----------------------+
```

- **Subscription processor** reads the mail sent to a plus address (`foo+bar@baz.com`), has an AI split each newsletter
  into topics and write a retelling of the long ones, and publishes every topic as its own item.
- **Feed hydrator** takes RSS/Atom feeds that only carry teasers, fetches the full article for each new entry and
  publishes the same feed with the full text in it.
- **Publisher** stores finished feeds as files and serves them over HTTP. It is the only thing that touches the web server.

The two sources run in parallel and know nothing about each other: the hydrator never looks at e-mail addresses, the
subscription processor never reads RSS. Configure either one, or both.

## Quick start

```bash
cp config.example.toml config.toml && chmod 600 config.toml  # all settings, including secrets and feed sources
chmod +x *.sh
./run.sh --once                                   # refresh every source once, write the feeds, exit
./run.sh                                          # run forever: web server + sources in parallel
```

Open `http://127.0.0.1:8787/` for a list of feeds and add their links to your RSS reader. The mail feed is
`mail.feed.xml`; hydrated feeds are named `<domain>-<path>.feed.xml`.
`--only hydrator` / `--only subscriptions` runs one source. `--once` does not start the web server. `--check` only checks
that the settings are usable (exit status 0 or 1) and changes nothing.

## Feed hydrator

Configured in `config.toml` under `[hydrator]` and `[[hydrator.feeds]]`:

```toml
[hydrator]
poll_minutes = 60

[[hydrator.feeds]]
url = "https://hnrss.org/frontpage"
selector = "auto"
summary = "start"
summary_label = ""
```

| Per feed | Meaning |
|---|---|
| `url` | The feed to enhance (Atom or RSS 2.0) |
| `selector` | Where the article text is on the article page: a CSS selector (`div.article`, `main > .post`, `article:has(h1)`, comma lists, see the list under "Cutting blocks out" for what is understood). **Every** outermost matching element is used, in page order, and concatenated. Or `"auto"` for feeds that link to arbitrary sites (see below) |
| `link_from` | Where the entry keeps the article URL: `"link"` (default: RSS `<link>`, Atom `<link rel="alternate">`) or `"id"` (RSS `<guid>` / Atom `<id>`) |
| `name` | Optional output file name (`something.feed.xml`); needed only if two feeds would get the same automatic name |
| `drop_unhydrated` | `true` (default): an entry that is not hydrated stays **out of the republished feed**: text posts, PDFs, skipped hosts, pages that yield too little text, entries given up on after three failed tries, and entries still waiting for their turn. The feed then contains only entries with their full text. `false` publishes them as the origin feed has them |
| `summary` | What happens to the entry's own summary (Atom `<summary>`, RSS `<description>`). Many readers show *either* the summary *or* the content, so there are three cases, see below: `"remove"` (default), `"start"`, `"end"` |
| `summary_label` | Label of the box a moved summary is shown in; default `"Summary"`. `""` (empty) gives plain text without the box, which suits a description that is not a summary (hnrss: Article URL, Comments URL, points) |
| `format` | `"original"` (default) publishes the feed in the format it came in. `"atom"` reformats an RSS 2.0 feed as Atom 1.0 (an Atom feed is left as it is), see below |
| `remove` | A list of cosmetic filters in uBlock Origin syntax that cut blocks out of the page *before* the text is extracted, e.g. `".article__block:has(div[data-type*=\"banner\"])"`. See "Cutting blocks out" below |
| `filters_file` | A text file (or a list of them) with such filters, one per line, as in a uBlock list. Relative paths are relative to the config file |
| `min_words` | A page that yields fewer words counts as a failed extraction. Default `0`, or `60` with `"auto"` |
| `skip_hosts` | Hosts whose links are never fetched. Default `[]`, or a list of video/social sites with `"auto"` |
| `skip_authors` | Entries whose author matches one of these rules are left out of the republished feed and never fetched (whatever `drop_unhydrated` says). Atom `<author>` (name and email) and RSS `<author>` / `<dc:creator>` are checked. Rules work as the mail source's `skip_keywords`: case-insensitive whole words, `*` stands for any letters, `read more` is a phrase, `/regex/i` is a regular expression. Added to the global `skip_authors` |
| `skip_authors_file` | A text file (or a list of them) with such rules, one per line, `!` starts a comment. Relative paths are relative to the config file |

| Global (all optional) | Default | Meaning |
|---|---|---|
| `poll_minutes` | `60` | How often every feed is checked |
| `request_delay` | `1.0` | Seconds between two article downloads, to be polite to the site |
| `max_items_per_run` | `30` | At most this many new articles are fetched per feed per pass (the rest next time) |
| `timeout` | `30` | Seconds per HTTP request |
| `allow_private_hosts` | `false` | Allow article links that point at loopback/private addresses (only useful for testing, see below) |
| `skip_authors`, `skip_authors_file` | none | As per feed, but for every feed |

**Output name:** `<domain>-<path>.feed.xml`, with `/` in the path turned into `-` and a trailing `.xml`/`.rss`/`.atom`
dropped, the query string ignored: `hnrss.org-frontpage.feed.xml`.

**Three cases for the summary** (`summary`)

| `summary` | Content of the entry | The entry's own `<summary>` / `<description>` |
|---|---|---|
| `"remove"` (default) | the full text fetched from the site and nothing else | **removed**: it usually repeats the opening of the text, so it is not needed |
| `"start"` | the summary first, in a labelled box, then a rule, then the full text | **removed**: moved into the content |
| `"end"` | the full text, then a rule, then the summary in the same box | **removed**: moved into the content |

The box is the one the AI summaries of the subscription processor use:
`<blockquote><strong>Summary:</strong> ...</blockquote>`. An RSS item without a `<title>` keeps its `<description>`,
because an item needs one of the two. Only entries that were hydrated are touched; entries that are published as
the origin has them (`drop_unhydrated: false`) keep their summary.

**Reformatting as Atom** (`"format": "atom"`)

Some readers handle Atom better, so each feed can be republished as Atom 1.0, based on `rss2atom.py`:

| RSS 2.0 | Atom 1.0 |
|---|---|
| channel title, link, description, language, copyright, image | `title`, `link rel="alternate"`, `subtitle`, `xml:lang`, `rights`, `logo` |
| lastBuildDate / pubDate | `updated` (else the newest entry's date) |
| item `content:encoded` | `content type="html"` |
| item `description` | `summary` (or nothing, if the summary was removed or moved into the content) |
| item `guid` / `link` | `id` (an IRI as it is, anything else a stable `urn:uuid`), `link rel="alternate"` |
| `comments`, `enclosure` | `link rel="replies"`, `link rel="enclosure"` |
| `author`, `dc:creator`, `category`, `pubDate` | `author`, `category`, `published` and `updated` |

The converted feed gets its own `rel="self"` link (its address here) and the feed id is that address. Elements from other
namespaces (`rian:`, `itunes:`, ...) are not carried over. The mail feed has the same switch:
`SUBSCRIPTION_FEED_FORMAT=atom` (its text becomes the Atom `<content>`).

**What changes in the feed**

- Atom: `<content>` is replaced with the full text (`type="html"`).
- RSS: the full text is added as `<content:encoded>`.
- Everything else stays, except the entry's own summary/description, which leaves its element in all three cases (see above).
  hnrss is the exception worth knowing: its description holds the Article URL, Comments URL and points, so use
  `"start"` or `"end"` there, not `"remove"`.
- The feed's own `rel="self"` link is pointed at the republished address, so a reader cannot wander back to the original.
- Namespaces and unknown extension elements (`rian:related`, `itunes:`, ...) are preserved. XML comments and
  stylesheet instructions are dropped.

### Cutting blocks out of the text (`remove`, uBlock Origin syntax)

Pages carry things you do not want in the feed: banners, "read also" links, newsletter pitches. `remove` takes filters in
the syntax of uBlock Origin's cosmetic filters. They are applied to the downloaded page *before* the text is extracted;
every element a filter matches is removed together with everything inside it.

```json
{
  "remove": ["##.cookie-banner"],
  "feeds": [{
    "url": "https://example.com/feed.xml",
    "selector": "div.article__block",
    "remove": [
      ".article__block:has(div[data-type*=\"banner\"])",
      ".article__block>strong>a",
      "news.example.com##.newsletter:has-text(/subscribe/i)",
      "~github.com##.sponsor"
    ],
    "filters_file": "filters.txt"
  }]
}
```

The first two lines are the usual case: drop every `.article__block` that contains a banner, and drop the links directly
inside `strong` inside a block. (A class needs its dot, as in uBlock: `article__block` without it would be an element
named `article__block`.) A `remove` at the top of the config applies to all feeds; the feed's own comes after it.

**Filter lines**

| Line | Meaning |
|---|---|
| `selector` or `##selector` | applies on every site |
| `a.com,b.com##selector` | only on these sites and their subdomains (the site is the article's, not the feed's, so one list can serve a feed that links everywhere, like hnrss) |
| `~a.com##selector`, `a.com,~b.a.com##selector` | everywhere except / on a.com but not on b.a.com |
| `a.com#@#selector` | exception: cancels a filter with exactly the same selector on a.com |
| `#?#` instead of `##` | accepted, same meaning |
| `! text`, blank lines, `[Adblock Plus 2.0]` | ignored |

**Selectors:** `tag`, `*`, `.class`, `#id`, `[attr]`, `[attr=v]`, `[attr~=v]`, `[attr|=v]`, `[attr^=v]`, `[attr$=v]`,
`[attr*=v]` (with an `i` flag), the combinators ` ` `>` `+` `~`, lists with `,`, and the pseudo-classes `:not()`, `:is()`,
`:where()`, `:has()` (also relative: `:has(> img)`, `:has(+ p)`), `:first-child`, `:last-child`, `:only-child`,
`:nth-child()`, `:nth-last-child()`, `:first-of-type`, `:last-of-type`, `:only-of-type`, `:nth-of-type()`,
`:nth-last-of-type()`, `:empty`.

**uBlock Origin's procedural operators**

| Operator | Meaning |
|---|---|
| `:has-text(text)` / `:has-text(/regex/flags)` / `:contains()` | the element's text matches; plain text is case-sensitive |
| `:matches-attr("name"="value")` | some attribute matches; plain text must match fully, `/regex/` anywhere |
| `:min-text-length(n)` | at least n characters of text |
| `:upward(n)` / `:upward(selector)` / `:nth-ancestor(n)` | remove the n-th / the nearest matching ancestor instead; the last step of a selector only |
| `:remove()` | accepted and ignored, matched elements are always removed |

Not supported, with an error that names the line: `:xpath()`, `:matches-css*()`, `:style()`, `:watch-attr()`, `:others()`,
scriptlets (`##+js(...)`), HTML filters (`##^`), network filters (`||example.com^`), wildcard and regex domains.
A parenthesis inside an argument must be balanced, escaped (`\)`), or sit in a quoted string.

All filters are matched against the page as it was before anything was removed, so their order does not matter. They
also apply in `"auto"` mode, before the text is looked for. Changing the filters discards the feed's cache and
hydrates it again, like changing the selector does.

### Feeds that link to arbitrary sites (Hacker News and similar)

For a feed like `hnrss.org` every entry links to a different site, so there is no selector to write. Use
`"selector": "auto"`, a "reader mode" that finds the main text of any article page on its own:

```json
{"url": "https://hnrss.org/frontpage", "selector": "auto", "summary": "start", "summary_label": ""}
```

- **How it decides:** navigation, sidebars, comments, cookie banners, share buttons and hidden elements are removed; if
  the page has exactly one substantial `<article>` that is the story, otherwise text blocks are scored by length, commas
  and link density and neighbouring blocks of the winner are merged. Link farms and widget-like blocks inside the
  result are dropped, then the same cleaning as for selectors is applied. It is a heuristic: expect good results on
  blogs, news sites and README pages, and misses on paywalls and pages built by JavaScript. A page that yields fewer
  than `min_words` (60) words counts as a failure; after three tries the entry is left out of the feed (`drop_unhydrated`).
- **Left out of the feed, without wasting a request:** a link that equals the entry's own `<comments>` URL (Ask HN and other text
  posts, where the description already is the text), anything that is not an HTML page (PDF, image), and hosts in
  `skip_hosts` (with `"auto"` by default YouTube, Vimeo, Twitch, X/Twitter, Instagram, Facebook, TikTok, LinkedIn).
- **`"summary": "start"`** with an empty **`summary_label`** moves what hnrss puts in each description (Article URL,
  Comments URL, points) above the extracted article as plain text, so the link to the discussion stays one click away.
- **Pick the right hnrss feed.** `/newest` is the firehose: only the latest 20 items, most with a single point, so an
  hourly pass would see a small part of it and fetch 20 random sites each time. `/frontpage`, `/best`, or
  `/newest?points=100&count=50` (hnrss.org documents `points`, `comments` and `count`) give a stream worth hydrating.
  The query string is not part of the automatic file name, so two variants of the same path need a `"name"`.
- **Safety.** The links come from strangers, so article fetches refuse loopback, private, link-local and carrier-grade-NAT
  addresses (your router, `localhost`, the LAN, Tailscale), including after redirects, and only follow `http`/`https`.
  A DNS-rebinding trick between the check and the connection is not covered. `allow_private_hosts` switches the guard
  off; use it only for testing against a local server. The feed URLs you configure yourself are not restricted.

**Only new items cost anything**

1. The feed is requested with `If-None-Match` / `If-Modified-Since`; a `304` ends the pass.
2. Otherwise a streaming scan reads nothing but the entry ids and compares them with the stored ones. No new id, no
   further work and no republishing.
3. Only entries not seen before are downloaded and extracted. The hydrated text of every entry is cached in
   `state/hydrator/<name>.json`, so old entries are never fetched or parsed again; entries that left the origin feed
   are forgotten.
4. An article that fails (HTTP error, selector matches nothing, too little text) is retried on the next passes, three
   tries in total (a site that is down for three passes counts), then given up. Non-articles (PDF, skipped hosts, text
   posts) are not retried at all. Entries that are not hydrated, whether given up or still waiting (for example beyond
   `max_items_per_run`), are left out of the republished feed unless `drop_unhydrated` is `false`; an entry that failed
   for good is therefore gone for good, and so is every entry of a feed whose selector stopped matching: watch the log.
5. Changing a feed's `selector`, `link_from`, `min_words`, `skip_hosts` or its filters (`remove`, `filters_file`) discards its cache and hydrates everything
   again; changing `summary`, `summary_label`, `drop_unhydrated` or `skip_authors` only rewrites the feed. The first start after an upgrade
   rewrites every feed once.

**Well-formed HTML for readers.** The extracted markup is rebuilt, not copied: unclosed `<p>`, `<li>`, `<td>` are
repaired, only a safe set of tags is kept (`p`, headings, lists, tables, `a`, `img`, `blockquote`, `figure`, `pre`,
`b`/`i`/..., `br`, `hr`), every attribute but `href`, `src`, `alt`, `colspan`, `rowspan` and `start` is removed,
relative links become absolute, lazy-loaded images (`data-src`, `srcset`) are resolved, 1x1 tracking pixels, scripts,
styles, forms and `javascript:` links are dropped and iframes become plain links. The result is valid XML-style markup.

## Subscription processor

Set `subscriptions.address` and `subscriptions.imap.password` in `config.toml`. Sign up for newsletters with the plus
address yourself; confirmation emails of double opt-in arrive as ordinary items.

- **Address filter:** headers of recent messages are fetched and the target address must match exactly (case-insensitive)
  in `To`, `Cc`, `Delivered-To`, `X-Original-To`, `Envelope-To` or `X-Forwarded-To`, so `foo@baz.com` and
  `foo+other@baz.com` are ignored and Bcc'd newsletters are found.
- **Topic splitting:** the email is flattened into numbered blocks; a first AI call returns only titles and block
  ranges, so the original wording and images are never rewritten. Boilerplate is left out.
- **Topic filters:** right after the split, before anything is summarised, a topic is dropped if its title contains a
  skip keyword (`SUBSCRIPTION_SKIP_KEYWORDS`, comma-separated, and/or `SUBSCRIPTION_SKIP_KEYWORDS_FILE`, one per line,
  `!` for comments) or if it is shorter than `SUBSCRIPTION_MIN_TOPIC_WORDS` words (default `0`, off). No AI call is spent
  on a dropped topic. Matching ignores case and looks at whole words: `sponsor` does not match "Sponsored", `sponsor*`
  does (a `*` stands for any letters), `*sponsor*` matches anywhere inside a word, `read more` is a phrase, 
  and `/^re:/i` is a regular expression (put it in the file if it needs a comma).
  Kept topics keep their position in the email in their id, and an email whose topics are all dropped still counts
  as processed. If the AI answer cannot be read, the whole email is one topic titled with its subject, which the
  filters see like any other title.
- **Summaries:** a second call per topic sends the full text and asks for a faithful retelling, not a description.

  | Topic length | Summary |
  |---|---|
  | shorter than `SUMMARY_LIMIT_WORDS` (500) | none, short enough to read as is |
  | from the first limit up to `SUMMARY_LONG_WORDS` (1000) | the size of the first limit (500 words) |
  | longer than the second limit | first limit x `SUMMARY_FACTOR` (250 words), to rein in big topics |

  `SUMMARY_RATIO` (for example `0.5`) additionally keeps every summary under that fraction of its topic. The size is a
  word budget in the prompt; a result more than 1.5x over budget is asked once more and the shorter one is kept.
- **Author:** every item gets a `dc:creator`: the sender's display name, plus ` : ` and the part of the subject before
  its first colon. `Matt Levine <...>` + `Money Stuff: Zuckerberg ...` -> `Matt Levine : Money Stuff`;
  `Bloomberg Businessweek <...>` + `How the Houthis ...` -> `Bloomberg Businessweek`. `Re:`/`Fwd:` are ignored, the colon
  must be followed by whitespace within the first 40 characters, and a prefix equal to the sender is not repeated.
- **Privacy of the address:** the feed shows the address masked (`f***+bar@b***.com`), and the mailbox address is replaced
  the same way anywhere it appears in item text or links (also URL-encoded). Tokens inside tracking links that merely
  *encode* the address cannot be detected.
- **Images:** hotlinked from the email; nothing is downloaded. `cid:` attachments, `data:` URIs and 1x1 pixels are dropped.

### AI providers

`AI_PROVIDER` = `anthropic`, `gemini`, `openai`, `deepseek`, `local` or `rapid-mlx`; if unset, the first key found is
used. Keys and model overrides live in the matching `[ai.*]` tables in `config.toml`. The hydrator does not use AI at all.

`local` and `rapid-mlx` share **one endpoint**, `LOCAL_BASE_URL` (default `http://127.0.0.1:8000/v1`), so a model server
that something else already runs there is reused. `local` never starts anything; `rapid-mlx` additionally runs
`rapid-mlx serve LOCAL_MODEL` when nothing answers (only when an email needs processing, stopped after the pass).

| What answers at `LOCAL_BASE_URL` | What happens |
|---|---|
| Nothing | `local`: error, retried next pass. `rapid-mlx`: started with `LOCAL_MODEL`, stopped after the pass |
| An OpenAI-compatible server serving `LOCAL_MODEL` | Used and never stopped |
| Another model | Used as is by default (the log says which); `LOCAL_ON_MISMATCH=fail` skips the email instead |
| An auth-protected server (401/403) | Used with `LOCAL_API_KEY` |
| Something unrelated | Error; nothing is started or killed |

Reasoning models spend part of the token budget on thinking; `<think>` blocks are stripped, and
`LOCAL_EXTRA_BODY='{"chat_template_kwargs":{"enable_thinking":false}}'` may switch thinking off. If rapid-mlx fails to
load the model, try `RAPID_MLX_ARGS="--no-mllm"`.

## Settings

All settings live in one `config.toml`, copied from `config.example.toml`. Python 3.12+'s standard-library `tomllib` reads it. TOML comments and native arrays are supported; secrets should be protected with `chmod 600 config.toml`.

- `[app]`: `port`, `host`, `public_url`, `data_dir`, `log_dir`, `access_log`.
- `[subscriptions]`: mailbox address, feed title/format, polling, topic filters and `[[subscriptions.skip_keywords]]` is not used: `skip_keywords` is a plain string array.
- `[subscriptions.imap]`: `host`, `user`, `password`, `folder`.
- `[subscriptions.summary]`: `limit_words`, `long_words`, `factor`, `ratio`.
- `[ai]` and `[ai.anthropic]`, `[ai.gemini]`, `[ai.openai]`, `[ai.deepseek]`, `[ai.local]`, `[ai.rapid_mlx]`: provider selection and credentials/model options.
- `[hydrator]`: global feed polling, extraction and filter settings.
- `[[hydrator.feeds]]`: one table per RSS/Atom source. Repeat it to add feeds.

Arrays are native TOML arrays, for example:

```toml
[subscriptions]
skip_keywords = ["sponsor*", "advertisement"]

[hydrator]
remove = ["##.cookie-banner", "##.advertisement"]
filters_file = ["filters/common.txt"]

[[hydrator.feeds]]
url = "https://hnrss.org/frontpage"
selector = "auto"
skip_hosts = ["youtube.com", "vimeo.com"]
remove = ["##.newsletter-popup"]
```

The complete list of options, defaults and comments is in `config.example.toml`. The hydrator does not use AI.

## Changing settings while it runs

The process watches `config.toml`. After an edit it checks the new configuration in a separate process (`--check`). If valid, running passes finish and the process replaces itself in place; if invalid, the error is logged and the old process keeps running. Filter files named by `filters_file` and `skip_authors_file` are read again on the next hydrator pass.

Data layout in `app.data_dir`: `feeds/*.feed.xml` (served), `state/subscriptions.json`, `state/hydrator/*.json`. Only `feeds/` is served.

Logs live in `app.log_dir` (default `~/Library/Logs/RssEnhancer/`): `RssEnhancer.log` (everything the program prints, written by the launchd agent), `stats.log` (summary size statistics) and `rapid-mlx.log`.

## Run in the background

```bash
./install-launchd.sh        # per-user launchd agent: starts at login, restarts on crash
./uninstall-launchd.sh      # remove it (feeds and state are kept)
tail -f ~/Library/Logs/RssEnhancer/RssEnhancer.log
```

Settings are read from `config.toml` next to the code, so no secrets end up in the plist.

## Hosting it elsewhere

Nothing needs inbound access to your machine: mail is read over outgoing IMAP, the AI API and the sites are called over
outgoing HTTPS. On a VPS or PaaS set `RSSENHANCER_HOST=0.0.0.0`, `RSSENHANCER_PUBLIC_URL`, an API provider
(`AI_PROVIDER=gemini` ...) and keep `RSSENHANCER_DIR` on persistent storage; without it the state is lost on restart
(the mail of the last days is summarised again, hydrated articles are fetched again). There is no HTTPS or
authentication: anyone who can reach the URL can read the full text of your feeds, so put a reverse proxy, a secret
path or a private network in front.

## Troubleshooting

`./run.sh --once` prints what each source did.

| You see | Likely cause |
|---|---|
| `subscriptions: not configured` / `hydrator: not configured` | Set `subscriptions.address`, or add a `[[hydrator.feeds]]` entry in `config.toml` |
| `selector 'div.x' matched nothing` | The class is not on the article page; check it in the browser's inspector. Looked up in the HTML the server sends, not the one JavaScript builds later |
| `only N words extracted, probably not an article` / `no article text found` | `"auto"` found nothing article-like (landing page, paywall, JavaScript app). The entry is left out of the feed (it keeps its original content with `drop_unhydrated: false`) |
| `refusing to fetch ...: ... is not a public address` | The link points into a private network; this is the safety guard |
| `HTTP 403` for articles | The site blocks scripts; there is no workaround here |
| `config.toml: feed #N: "remove" #M ...: ...` | A filter could not be read or uses something that is not supported (see the list under "Cutting blocks out"); the line and the position are named |
| `unsupported feed format` | Only Atom and RSS 2.0 are handled (not RSS 1.0/RDF) |
| `poll failed: ... AUTHENTICATIONFAILED` | Wrong `IMAP_USER`/`IMAP_PASSWORD`; Gmail needs an app password and IMAP enabled |
| `0 message(s) since ...` | Mail older than `SUBSCRIPTION_LOOKBACK_DAYS` or not in `IMAP_FOLDER` (Gmail: `"[Gmail]/All Mail"`) |
| `0 of N ... addressed to ...` plus recipients | Nothing was sent to exactly that plus address; compare the list with `SUBSCRIPTION_ADDRESS` |
| `no model server answering at ...` | `AI_PROVIDER=local` never starts a server; use `rapid-mlx` or start one |

## Limitations

- The hydrator extracts from the server-rendered HTML; pages that build the text with JavaScript are out of reach.
- `"auto"` is a heuristic, not a replacement for a dedicated extraction service; a reader's own Reader view may do better
  on some pages. Hydrating pays off when you read the feed in several apps or want the text in the feed itself.
- Selectors and filters see the HTML the server sends, not a rendered page: no `:matches-css()`, no `:xpath()`, nothing that depends on JavaScript or layout.
- Mail HTML is linearised: table layouts and styling are dropped; text, links, bold/italic and images are kept.
- One mailbox address per instance; run several copies with different `RSSENHANCER_ENV`/`RSSENHANCER_DIR`/`RSSENHANCER_PORT` for more.
- Respect the terms of the sites you hydrate; a republished full-text feed is meant for your own reading.

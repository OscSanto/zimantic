import codecs
import json
import posixpath
import re
import time
from html.parser import HTMLParser
from urllib.parse import unquote
from typing import Any, Iterator, NamedTuple, Protocol, cast
from libzim.reader import Archive, set_cluster_cache_max_size

# libzim undercounts this cache: its 16 MB default really used ~170 MB.
# 1 MB used ~4 MB and was not slower.
set_cluster_cache_max_size(1 << 20) # 1*2^20 = ~1MB

# Best-effort budget for one entry parse (HTML or PDF). Parsers check a shared
# deadline cooperatively between chunks/pages and return the partial result,
# so an adversarial page slows a request down instead of hanging it. The
# builder passes no timeout (offline; skipping entries would silently degrade
# the index); the runtime preview path uses this default.
DEFAULT_PARSE_TIMEOUT = 5.0

# Parsing bookkeeping keeps one open block per candidate tag. Beyond this many
# nested containers the page is not prose: stop accumulating text for the outer
# blocks (marking them overflow) so per-chunk work stays bounded instead of
# growing with nesting depth.
MAX_ACTIVE_DEPTH = 200


class EntryRecord(NamedTuple):
    """Indexable fields extracted from one ZIM entry."""

    id: int
    title: str
    excerpt: str
    path: str
    target_id: int | None


class EntrySource(Protocol):
    """The part of a libzim ``Archive`` needed to read one entry by id.

    Companion lookups (``has_entry_by_path``/``get_entry_by_path``) only happen
    for app-shell pages, so keeping this seam narrow lets tests supply small
    in-memory archives without reimplementing the whole ``Archive`` API.
    """

    def _get_entry_by_id(self, entry_id: int, /) -> Any: ...


MIN_BLOCK_CHARS = 50    # shorter blocks are ignored; the page remains searchable by title
DEFAULT_PREVIEW_CHARS = 1000
DEFAULT_MAX_HTML_BYTES = 4 << 20
# This protects the builder from pathological decompressed entries while still
# accepting genuinely large articles by default. Profiles can lower it for
# memory-constrained machines.
DEFAULT_MAX_ENTRY_BYTES = 1 << 30
# text/html is the common case; XHTML parses the same way, plain text is split
# into an excerpt with a simple normalization, and PDFs are read through their
# embedded text layer.
_READABLE_MIMETYPES = frozenset({
    "text/html", "application/xhtml+xml", "text/plain", "application/pdf",
})
# Plain-text entries are usually article text (a text-based ZIM), but asset
# bundles also ship licenses, dotfiles and config as text/plain. Skip those so
# they never become search results. License/readme names are matched only in
# their conventional ALL CAPS form so a plain-text article such as
# "License_to_Wed" or a dictionary entry named "license" is still indexed.
_PLAIN_TEXT_ASSET = re.compile(
    r"(^|/)\.[^/]*$"  # dotfiles such as .gitignore
    # Non-prose, machine-readable or media files (with an optional ?query/#frag).
    r"|(?i:\.(?:md|markdown|rst|json|jsonl|ya?ml|toml|ini|cfg|conf|js|mjs|cjs"
    r"|css|scss|less|xml|svg|png|jpe?g|gif|webp|ico|csv|tsv|log|lock|map"
    r"|handlebars|hbs|mustache|twig|liquid|jade|pug|ejs|tmpl|tpl))"
    r"(?:[?#][^/]*)?$"
    # License/notice files, which are conventionally ALL CAPS.
    r"|(^|/)(?:COPYING|LICEN[CS]E|PATENTS?|NOTICE|README|CHANGELOG|AUTHORS"
    r"|CONTRIBUTORS|MAKEFILE|DOCKERFILE)(?:[._-]|$)"
    # Anything inside a bundle's asset directory.
    r"|(?i:(^|/)(?:assets?|static|_static|_assets|media|vendor|node_modules"
    r"|mathjax|fonts?|images?|img|css|js|scripts?|downloads?)/)"
)
# HTML entries that are really assets with a wrong MIME type: an unmistakable
# non-HTML extension (scripts, stylesheets, JSON data, media). zimit crawls
# sometimes label a <site>.js or <site>.css as text/html, which would otherwise
# become a title-only search result.
_HTML_ASSET_PATH = re.compile(
    r"\.(?:js|mjs|cjs|css|scss|less|json|xml|svg|png|jpe?g|gif|webp|ico|map"
    r"|woff2?|ttf|otf|eot|mp3|mp4|webm|pdf)$",
    re.I,
)
REFRESH_SCAN_BYTES = 64 << 10
REFRESH_CONTENT = re.compile(r"^\s*0\s*;\s*url\s*=\s*(.*?)\s*$", re.I)
BOILERPLATE = re.compile(
    r"This article or its section is a stub\."
    r"|You can help by expanding the article\."
    r"|Our robots\.txt blocks googlebot\."
    r"|You're wasting your own time by spamming here\.",
    re.I,
)

# Some ZIMs render every article through a JavaScript app (an "SPA shell"): the
# HTML entry is a tiny stub that meta-refreshes into an app route such as
# index.html#/Bookshelves/Subject/Page, while the real body lives in a companion
# JSON file. Read only the stub and every article collapses onto the shell
# (title "index.html", excerpt "enable JavaScript"); read the JSON and the
# article is searchable by title, full text, and meaning, and its own ZIM path
# deep-links into the app route.
SPA_PAGE_ID = re.compile(r"_(\d+)$")
SPA_CONTENT_PATH = "content/page_content_{id}.json"
# The companion body is normally under "htmlBody"; accept the other keys some
# shells use so more app-shell ZIMs become searchable. "description" is plain
# text (video/playlist/channel metadata) rather than HTML.
SPA_BODY_KEYS = ("htmlBody", "body", "html", "content", "text", "description")
# Not every app bundle uses Kiwix's content JSON. Media bundles (for example
# youtube2zim) keep a "<slug>.json" beside the route, commonly under these
# directories; <slug> is the deep link's last path segment.
SPA_COMPANION_DIRS = ("", "videos", "playlists", "channels", "content", "pages", "posts", "items")
# A tag-like "<p ...>", "<br/>" or "<!doctype" (but not prose such as "a < b").
_HTML_TAG = re.compile(rb"<[a-zA-Z!/][^>]*>")
# A "lang-xx" class token. Pages that ship the same text in many languages
# (TED talks, and other parallel-translation scrapes) mark each copy this way;
# the "lang-default" copy is the document's primary language.
_ELEMENT_LANG = re.compile(r"(?:^|\s)lang-([a-z0-9-]+)")
# Markdown front-matter ("---\n...title: ...\n---") and syntax. ZIMs built from
# a docs/curriculum source (freeCodeCamp and friends) ship the article body as
# text/markdown; the HTML route is only a meta-refresh stub, so the markdown is
# read as the stub's companion.
_YAML_FRONTMATTER = re.compile(r"\A---[ \t]*\r?\n(.*?)\r?\n---[ \t]*\r?\n", re.S)
_MARKDOWN_FENCE = re.compile(r"```.*?```", re.S)
_MARKDOWN_SECTION = re.compile(r"(?m)^#{1,6}[ \t]*--[a-z0-9-]+--[ \t]*$")
_MARKDOWN_LINK = re.compile(r"!?\[([^\]]*)\]\([^)]*\)")
_MARKDOWN_HTML = re.compile(r"<[^>]+>")
_MARKDOWN_HEADING = re.compile(r"(?m)^[ \t]{0,3}#{1,6}[ \t]*")
_MARKDOWN_MARKER = re.compile(r"(?m)^[ \t]{0,3}(?:[-*+]|\d+[.)])[ \t]+")
JS_SHELL_MARKERS = (b"<noscript", b'id="app"', b"id='app'")
JS_NOTICE_MAX_CHARS = 300

# Page chrome that is navigation, maintenance or boilerplate rather than article
# text. Matching these by id, class token or ARIA role keeps category footers,
# navboxes and tables of contents out of excerpts (and therefore out of the
# embedding), which is a large share of the text on MediaWiki stubs.
SKIP_IDS = frozenset({
    "catlinks", "mw-hidden-catlinks", "printfooter", "footer",
    "toc", "mw-navigation", "sitefooter", "breadcrumbs",
})
SKIP_CLASSES = frozenset({
    "catlinks", "mw-hidden-catlinks", "mw-hidden-cats-hidden",
    "navbox", "vertical-navbox", "navbox-inner", "navbox-styles", "navbar",
    "metadata", "ambox", "mbox", "ombox", "messagebox",
    "toc", "toccolours",
    "mw-editsection", "hatnote", "reflist", "references",
    "mw-references-wrap", "noprint", "mw-jump-link",
    "portal", "sistersitebox", "sidebar",
    "shortdescription", "noindex", "printfooter", "stub", "boilerplate",
    "assistive", "visuallyhidden", "screen-reader-text", "sr-only",
    # Breadcrumbs and language pickers (PHP manual and similar).
    "breadcrumb", "breadcrumbs", "change-language",
    # LibreTexts/MindTouch app-shell topic listings.
    "mt-topic-hierarchy-listings", "mt-guide-listings", "mt-listing-detailed",
    # DevDocs/Sphinx attribution footer ("© … Licensed under …").
    "_attribution",
})
SKIP_ROLES = frozenset({"navigation", "banner", "contentinfo"})

# Persist disambiguation pages with a title suffix instead of a metadata table.
DISAMBIGUATION_SUFFIX = " (disambiguation)"
DISAMBIG_TITLE = re.compile(r"\s*\(disambiguation\)\s*$", re.I)
DISAMBIG_BOILERPLATE = re.compile(r"this disambiguation page", re.I)
DISAMBIG_HTML_MARKERS = re.compile(
    rb"this disambiguation page"          # template footer, wherever it renders
    rb"|category:[^\"'<>\s]{0,120}?disambig",  # rendered Category:…disambiguation link
    re.I,
)
WGCATEGORIES = re.compile(rb'"wgCategories"\s*:\s*(\[[^\]]*\])')

class _MetaRefresh(HTMLParser):
    def __init__(self):
        super().__init__()
        self.url = None

    def handle_starttag(self, tag, attrs):
        if self.url is not None or tag != "meta":
            return

        attributes = dict(attrs)
        http_equiv = (attributes.get("http-equiv") or "").strip().lower()
        content = attributes.get("content") or ""
        if http_equiv != "refresh":
            return

        match = REFRESH_CONTENT.fullmatch(content)
        if not match:
            return

        url = match.group(1).strip()
        if len(url) >= 2 and url[0] == url[-1] and url[0] in "'\"":
            url = url[1:-1].strip()
        if url:
            self.url = url


def _refresh_url(html: bytes):
    parser = _MetaRefresh()
    parser.feed(html[:REFRESH_SCAN_BYTES].decode("utf-8", "ignore"))
    return parser.url


class _TextExtractor(HTMLParser):
    """Collect prioritized, visible text blocks from an HTML page."""

    CANDIDATE_PRIORITIES = {
        "p": 0,
        "blockquote": 1,
        "pre": 1,
        "div": 1,
        "section": 1,
        "article": 1,
        "main": 1,
        "ol": 2,
        "li": 2,
    }
    SKIP_TAGS = {"head", "script", "style", "template"}
    CHROME_TAGS = {"aside", "footer", "header", "nav"}
    # Tags that never have a closing tag; while skipping a container we must not
    # push them or the open/close bookkeeping would drift.
    VOID_TAGS = frozenset({
        "area", "base", "br", "col", "embed", "hr", "img", "input",
        "link", "meta", "param", "source", "track", "wbr",
    })
    # Block-level tags end one text run and begin another. Inserting a space at
    # each boundary stops runs of text from concatenating when the source has no
    # whitespace between elements (headings, <dt>/<dd>, list links, and so on).
    SEPARATOR_TAGS = {
        "p", "blockquote", "pre", "div", "section", "article", "main",
        "ol", "ul", "li", "dl", "dt", "dd",
        "table", "thead", "tbody", "tfoot", "tr", "th", "td", "caption",
        "h1", "h2", "h3", "h4", "h5", "h6",
        "figure", "figcaption", "address", "hr", "br",
        "details", "summary", "form", "fieldset", "legend",
    }

    def __init__(self):
        super().__init__()
        self.skipping = []  # open tags whose contents are not article text
        self.blocks = []
        self.active = []
        self.block_order = 0
        # Early termination bookkeeping (see _can_stop): a lang-* class disables
        # it, and _priority0_chars tracks closed top-priority (p) blocks.
        self._saw_lang = False
        self._priority0_chars = 0

    @staticmethod
    def _is_chrome(tag, attributes) -> bool:
        """True for navigation, maintenance or boilerplate containers."""
        if tag in _TextExtractor.SKIP_TAGS or tag in _TextExtractor.CHROME_TAGS:
            return True
        if tag == "sup" and "reference" in (attributes.get("class") or ""):
            return True
        if (attributes.get("aria-hidden") or "").casefold() == "true":
            return True
        if "hidden" in attributes:
            return True
        style = (attributes.get("style") or "").replace(" ", "").casefold()
        if "display:none" in style or "visibility:hidden" in style:
            return True
        if (attributes.get("role") or "").casefold() in SKIP_ROLES:
            return True
        identifier = (attributes.get("id") or "").casefold()
        if identifier in SKIP_IDS or identifier.endswith("footer"):
            return True
        classes = (attributes.get("class") or "").casefold().split()
        return any(
            name in SKIP_CLASSES or name.endswith("footer") for name in classes
        )

    def _separate(self) -> None:
        for block in self.active:
            if not block.get("overflow"):
                block["parts"].append(" ")

    def handle_starttag(self, tag, attrs):
        if self.skipping:
            # Track nested tags so the matching close ends the skip, not the
            # first nested close of the same name (a footer full of <div>s).
            if tag not in self.VOID_TAGS:
                self.skipping.append(tag)
            return
        attributes = dict(attrs)
        if self._is_chrome(tag, attributes):
            if tag not in self.VOID_TAGS:
                self.skipping.append(tag)
            return

        if tag in self.SEPARATOR_TAGS and self.active:
            self._separate()
        if tag in self.CANDIDATE_PRIORITIES:
            block = {
                "tag": tag,
                "priority": self.CANDIDATE_PRIORITIES[tag],
                "order": self.block_order,
                "nested": False,
                # A container opened past the nesting budget cannot be article
                # text; it tracks nesting only, so text and separators skip it.
                "overflow": len(self.active) >= MAX_ACTIVE_DEPTH,
                "parts": [],
            }
            match = _ELEMENT_LANG.search(attributes.get("class") or "")
            if match:
                block["lang"] = match.group(1)
                self._saw_lang = True
            self.blocks.append(block)
            self.active.append(block)
            self.block_order += 1

    def handle_endtag(self, tag):
        if self.skipping:
            if tag in self.VOID_TAGS:
                return
            if tag == self.skipping[-1]:
                self.skipping.pop()
            elif tag in self.skipping:  # mismatched nesting: unwind to the match
                while self.skipping and self.skipping[-1] != tag:
                    self.skipping.pop()
                if self.skipping:
                    self.skipping.pop()
            return
        if tag in self.CANDIDATE_PRIORITIES:
            for index in range(len(self.active) - 1, -1, -1):
                if self.active[index]["tag"] == tag:
                    block = self.active.pop(index)
                    text = self._text(block["parts"])
                    block["text"] = text
                    if len(text) >= MIN_BLOCK_CHARS:
                        for parent in self.active[:index]:
                            parent["nested"] = True
                        if block["priority"] == 0 and not block["nested"]:
                            self._priority0_chars += len(text)
                    break
        if tag in self.SEPARATOR_TAGS and self.active:
            self._separate()

    def handle_data(self, data):
        if self.skipping:
            return
        for block in self.active:
            if not block.get("overflow"):
                block["parts"].append(data)

    @staticmethod
    def _text(parts):
        text = BOILERPLATE.sub(" ", "".join(parts))
        return " ".join(text.split())

    def candidates(self) -> list[str]:
        # When the page repeats its text under lang-* classes, keep only the
        # lang-default copy (the document's primary language) so parallel
        # translations don't crowd the excerpt.
        langs = {block["lang"] for block in self.blocks if block.get("lang")}
        default_lang = "default" if "default" in langs else None
        candidates = []
        for block in self.blocks:
            if block["nested"] or block.get("overflow"):
                continue
            if default_lang is not None and block.get("lang") not in (None, default_lang):
                continue
            text = block.get("text", self._text(block["parts"]))
            if len(text) >= MIN_BLOCK_CHARS:
                candidates.append((block["priority"], block["order"], text))
        return [
            text
            for _, _, text in sorted(candidates, key=lambda candidate: candidate[:2])
    ]

    def _can_stop(self, max_chars: int) -> bool:
        """True once parsing the rest of the page cannot change the excerpt.

        The excerpt is the opening blocks in (priority, order) order, filled to
        ``max_chars``. Once the closed top-priority blocks already sum past that
        budget, no later block can sort before them (top priority is the minimum
        and order only grows), so the lead alone determines the result. We stop
        only when every still-open block is already marked nested: an open
        non-nested container could otherwise still yield a candidate later.
        Overflow containers are treated as satisfied (they never yield text).
        """
        if self._saw_lang or self._priority0_chars < max_chars:
            return False
        return all(
            block["nested"] or block.get("overflow") for block in self.active
        )


def is_disambiguation(title: str, text: str, html: bytes = b"") -> bool:
    """True for a MediaWiki disambiguation page.

    Signals, any of which is enough: a "(disambiguation)" title suffix, the
    "This disambiguation page" template footer, or a disambiguation category
    (rendered link or wgCategories). ``text`` is checked first for the footer so
    template hubs never need the raw-HTML scan.
    """
    if DISAMBIG_TITLE.search(title):
        return True
    if DISAMBIG_BOILERPLATE.search(text):
        return True
    if not html:
        return False
    if DISAMBIG_HTML_MARKERS.search(html):
        return True
    categories = WGCATEGORIES.search(html)
    return bool(categories and re.search(rb"disambig", categories.group(1), re.I))


def disambiguation_title(title: str) -> str:
    """Return the stored title for a disambiguation page."""
    return title if DISAMBIG_TITLE.search(title) else title + DISAMBIGUATION_SUFFIX


# Bytes of HTML per feed() call. Small enough that early termination (see
# _TextExtractor._can_stop) stops near the lead of a typical article, rather
# than only after the whole page has already been fed.
CHUNK = 8192

# <script> and <style> content is never article text, and it can be tens of KB
# of CSS/JS. Stripping it up front also sidesteps html.parser's CDATA handling,
# which misbehaves when a single very large block is fed in small chunks.
_SCRIPT_STYLE = re.compile(rb"<(script|style)\b[^>]*>.*?</\1>", re.I | re.S)

# <head> (and a stray <title>) carry metadata, not visible text. They are
# stripped before the loader-shell emptiness check so an ordinary <title> does
# not hide the fact that the page body has no text (see _is_loader_shell).
_HEAD = re.compile(rb"<head\b[^>]*>.*?</head>", re.I | re.S)
_TITLE = re.compile(rb"<title\b[^>]*>.*?</title>", re.I | re.S)


def iter_text_blocks(
    html: bytes,
    max_chars: int | None = None,
    deadline: float | None = None,
) -> Iterator[str]:
    """Yield substantial article blocks, preferring paragraphs over fallbacks.

    When ``max_chars`` is given, parsing stops as soon as the opening blocks
    provably fill that budget (see ``_TextExtractor._can_stop``), so a long page
    costs only its lead rather than the whole document. ``deadline`` is a
    ``time.monotonic()`` budget: parsing stops between chunks once it passes and
    yields whatever text was collected, so a pathological page cannot hang a
    request.
    """
    parser = _TextExtractor()
    html = _SCRIPT_STYLE.sub(b" ", html)

    # "ignore" drops bytes that aren't valid UTF-8 rather than crashing.
    decoder = codecs.getincrementaldecoder("utf-8")("ignore")
    for start in range(0, len(html), CHUNK):
        parser.feed(decoder.decode(html[start:start + CHUNK]))
        if deadline is not None and time.monotonic() >= deadline:
            break
        if max_chars is not None and parser._can_stop(max_chars):
            break
    parser.feed(decoder.decode(b"", final=True))
    yield from parser.candidates()


def truncate_at_word_boundary(text: str, max_chars: int) -> str:
    """Limit text without cutting through a word when a boundary is available."""
    limit = max(1, int(max_chars))
    if len(text) <= limit:
        return text
    cut = text[:limit]
    boundary = max(cut.rfind(" "), cut.rfind("\n"), cut.rfind("\t"))
    return cut[:boundary].rstrip() if boundary > 0 else cut


def _join_blocks(candidates: list[str], max_chars: int) -> str:
    """Join the opening blocks up to ``max_chars``, truncating the last one.

    The bounded text is used for the embedding during indexing and can also be
    used as a preview during runtime result hydration. The model truncates it
    to ``embedding_tokens`` when it builds the vector, so there is no need to
    compute a separate token-budgeted text here; measured on real indexes this
    loses only a few characters of model input per article.
    """
    current = ""
    for candidate in candidates:
        separator = "\n\n" if current else ""
        available = max_chars - len(current) - len(separator)
        if available <= 0:
            break
        if len(candidate) <= available:
            current += separator + candidate
            continue
        fitted = truncate_at_word_boundary(candidate, available)
        if fitted:
            current += separator + fitted
        break
    # Nothing fit: keep a truncated opening block so the page still has a preview.
    return current or truncate_at_word_boundary(candidates[0], max_chars)


def extract_excerpt(
    html: bytes,
    max_preview_chars: int = DEFAULT_PREVIEW_CHARS,
    deadline: float | None = None,
) -> str:
    """Return the article's opening text, bounded to ``max_preview_chars``.

    The returned text is used as the UI preview at query time and as the
    embedding input during indexing; the embedder truncates it to
    ``embedding_tokens`` when it creates the vector. ``deadline`` bounds the
    parse cooperatively (see ``iter_text_blocks``).
    """
    max_chars = max(1, int(max_preview_chars))
    candidates = list(iter_text_blocks(html, max_chars=max_chars, deadline=deadline))
    if not candidates:
        return ""
    return _join_blocks(candidates, max_chars)


def _json_body(zim: Archive, path: str) -> bytes | None:
    """Return a text body from the JSON entry at ``path``, or None."""
    if not path or not zim.has_entry_by_path(path):
        return None
    try:
        payload = json.loads(bytes(zim.get_entry_by_path(path).get_item().content))
    except (LookupError, RuntimeError, TypeError, ValueError):
        return None
    if not isinstance(payload, dict):
        return None
    for key in SPA_BODY_KEYS:
        body = payload.get(key)
        if isinstance(body, str) and body.strip():
            return body.encode("utf-8")
    return None


def _fragment_slug(fragment: str) -> str:
    """Last path segment of an app deep link, query/hash stripped.

    ``/watch/getting-started-abc`` and ``/playlist/sql-basics`` yield
    ``getting-started-abc`` and ``sql-basics``.
    """
    path = fragment.split("?", 1)[0].split("#", 1)[0]
    parts = [part for part in path.split("/") if part]
    return parts[-1] if parts else ""


# Directories where a markdown companion for an app-shell route may live.
MARKDOWN_COMPANION_DIRS = ("", "content/curriculum", "curriculum", "content", "pages", "docs")
MARKDOWN_EXTENSIONS = (".md", ".markdown")
MARKDOWN_MIME_TYPES = frozenset({
    "text/markdown",
    "text/x-markdown",
    "text/plain",
    # Some freeCodeCamp ZIMs label Markdown companions as JavaScript.
    "application/javascript",
})


def _markdown_source(zim: Archive, path: str) -> bytes | None:
    """Return the entry at ``path`` when it is a Markdown source, else None.

    Companions are recognized by their common Markdown extension or MIME type;
    ZIM packaging does not always keep those signals consistent.
    """
    if not path or not zim.has_entry_by_path(path):
        return None
    item = zim.get_entry_by_path(path).get_item()
    mimetype = item.mimetype.split(";", 1)[0].strip().lower()
    extension = posixpath.splitext(path)[1].lower()
    if extension in MARKDOWN_EXTENSIONS or mimetype in MARKDOWN_MIME_TYPES:
        return bytes(item.content)
    return None


def _companion(zim: Archive, entry, fragment: str):
    """Return an app-shell stub's companion body, or None.

    Returns ``("body", bytes)`` for a JSON companion whose text may be HTML or
    plain text, or ``("markdown", bytes)`` for a Markdown source. Several
    conventions are tried, most specific first:

    * Kiwix's ``content/page_content_<id>.json`` keyed by the numeric page id.
    * Media bundles that keep a ``<slug>.json`` companion for the deep link (for
      example ``videos/<slug>.json`` or ``playlists/<slug>.json``), or beside
      the stub entry itself.
    * Docs/curriculum bundles whose ``<route>.md`` holds the body (freeCodeCamp
      and similar), under the usual content directories or beside the stub.
    """
    slug = _fragment_slug(fragment)
    base = posixpath.dirname(entry.path)

    json_candidates = []
    match = SPA_PAGE_ID.search(entry.path)
    if match:
        json_candidates.append(SPA_CONTENT_PATH.format(id=match.group(1)))
    if slug:
        for directory in SPA_COMPANION_DIRS:
            json_candidates.append(
                posixpath.join(directory, slug + ".json") if directory else slug + ".json"
            )
        json_candidates.append(posixpath.join(base, slug + ".json"))
    for candidate in json_candidates:
        body = _json_body(zim, candidate)
        if body is not None:
            return ("body", body)

    route = fragment.split("?", 1)[0].split("#", 1)[0].strip("/")
    if not route:
        return None
    names = (route, posixpath.basename(route))
    markdown_candidates = []
    for directory in MARKDOWN_COMPANION_DIRS:
        for name in names:
            for extension in MARKDOWN_EXTENSIONS:
                markdown_candidates.append(
                    posixpath.join(directory, name + extension)
                    if directory
                    else name + extension
                )
            markdown_candidates.append(
                posixpath.join(directory, name) if directory else name
            )
    markdown_candidates.extend(
        posixpath.join(base, names[1] + extension)
        for extension in MARKDOWN_EXTENSIONS
    )
    markdown_candidates.append(posixpath.join(base, names[1]))
    for candidate in markdown_candidates:
        source = _markdown_source(zim, candidate)
        if source is not None:
            return ("markdown", source)
    return None


def _meta_description(html: bytes) -> str:
    """Return a meta/Open Graph description from the page head, if any.

    Used only as a fallback when no visible text blocks were found, so thin
    pages still get a preview and an embedding instead of being title-only.
    """
    best = ""
    for match in re.finditer(rb"<meta\b[^>]*>", html, re.I):
        tag = match.group(0)
        if not re.search(rb"(?:name|property)\s*=\s*[\"']?\s*(?:og:)?description\b", tag, re.I):
            continue
        content = re.search(rb"content\s*=\s*\"([^\"]*)\"", tag, re.I) or re.search(
            rb"content\s*=\s*'([^']*)'", tag, re.I
        )
        if content is None:
            continue
        text = content.group(1).decode("utf-8", "ignore")
        if len(text) > len(best):
            best = text
    return " ".join(best.split())


def is_javascript_shell(html: bytes, text: str) -> bool:
    """True when a page is only an "enable JavaScript" app shell.

    Such pages carry no article text. Indexing them pollutes meaning search with
    a boilerplate vector, and because they can share one shell target they also
    surface each other. The notice must be short, mention JavaScript, and sit
    beside a shell marker so a real article that merely discusses JavaScript is
    not dropped.
    """
    if not isinstance(text, str) or len(text) > JS_NOTICE_MAX_CHARS:
        return False
    folded = text.casefold()
    if "javascript" not in folded or ("enable" not in folded and "disabled" not in folded):
        return False
    return any(marker in html for marker in JS_SHELL_MARKERS)


def _is_loader_shell(html: bytes) -> bool:
    """True when a page ships no visible text, only script/style/link references.

    A JavaScript app that renders content it does not ship (PhET simulations and
    similar) has a body of only <script>/<link>/<style> elements and a title.
    Such pages are kept as title-only entries: giving them a shared meta
    description as the excerpt would collapse every article onto the shell.

    Head metadata (title, meta, ...) is not visible text, so it is stripped
    before checking for emptiness; otherwise the usual <title> would make every
    page look like it had a body and the check would never fire.
    """
    stripped = _HEAD.sub(b" ", html)
    stripped = _TITLE.sub(b" ", stripped)
    stripped = _SCRIPT_STYLE.sub(b" ", stripped)
    stripped = re.sub(rb"<[^>]+>", b" ", stripped)
    if stripped.strip():
        return False
    return (b"<script" in html or b"<link" in html) and bool(_HTML_TAG.search(html))


def _plain_text_excerpt(raw: bytes, max_chars: int) -> str:
    """Turn a non-HTML text entry into a single normalized excerpt."""
    text = raw.decode("utf-8", "ignore")
    return truncate_at_word_boundary(" ".join(text.split()), max_chars)


def _markdown_excerpt(
    raw: bytes,
    max_chars: int,
    deadline: float | None = None,
) -> tuple[str, str]:
    """Return ``(excerpt, front_matter_title)`` for a markdown document.

    Front matter and fenced code are dropped, section headings such as
    ``# --instructions--`` and list markers are removed, and link/image syntax
    is reduced to its label, so the excerpt is the prose a reader sees.
    ``deadline`` skips any remaining cleanup steps once the parse budget is
    spent, keeping the text normalized so far.
    """
    text = raw.decode("utf-8", "ignore")
    title = ""
    front = _YAML_FRONTMATTER.match(text)
    if front:
        title_match = re.search(r"(?m)^title[ \t]*:[ \t]*(.+?)[ \t]*$", front.group(1))
        if title_match:
            title = title_match.group(1).strip().strip("'\"")
        text = text[front.end():]

    def expired() -> bool:
        return deadline is not None and time.monotonic() >= deadline

    if not expired():
        text = _MARKDOWN_FENCE.sub(" ", text)
    if not expired():
        text = _MARKDOWN_SECTION.sub(" ", text)
    if not expired():
        text = _MARKDOWN_LINK.sub(r"\1", text)
    if not expired():
        text = _MARKDOWN_HTML.sub(" ", text)
    if not expired():
        text = _MARKDOWN_HEADING.sub("", text)
    if not expired():
        text = _MARKDOWN_MARKER.sub("", text)
        text = text.replace("`", "").replace("*", "").replace("~~", "")
    return truncate_at_word_boundary(" ".join(text.split()), max_chars), title


def _pdf_module():
    """Return the PyMuPDF module, imported lazily, or None when unavailable.

    PDFs are a small share of most ZIMs, so MuPDF's native library is loaded only
    the first time one is actually read. A server that never touches a PDF (or a
    build of a PDF-free ZIM) never pays for it.
    """
    try:
        import pymupdf
    except ImportError:  # pragma: no cover - pymupdf is a declared dependency
        return None
    return pymupdf


def extract_pdf_excerpt(
    raw: bytes,
    max_preview_chars: int = DEFAULT_PREVIEW_CHARS,
    deadline: float | None = None,
) -> str:
    """Return a PDF's opening text, bounded to ``max_preview_chars``.

    Only whole pages are read, and only until the character budget is filled, so
    a long document costs little. A scanned PDF has no text layer and yields an
    empty string, which the caller treats as "not indexable" rather than storing
    a title-only or boilerplate entry. A password-protected or malformed PDF is
    skipped the same way instead of failing the build. ``deadline`` bounds the
    page loop so a hostile PDF cannot monopolize a request.
    """
    if not raw:
        return ""
    pymupdf = _pdf_module()
    if pymupdf is None:
        return ""
    max_chars = max(1, int(max_preview_chars))
    try:
        document = pymupdf.open(stream=raw, filetype="pdf")
    except Exception:
        return ""
    try:
        if document.needs_pass:
            return ""
        pieces: list[str] = []
        total = 0
        for page in document:
            if deadline is not None and time.monotonic() >= deadline:
                break
            try:
                text = page.get_text("text")
            except Exception:
                continue
            normalized = " ".join(text.split())
            if not normalized:
                continue
            pieces.append(normalized)
            total += len(normalized) + 1
            if total >= max_chars:
                break
    finally:
        document.close()
    return truncate_at_word_boundary(" ".join(pieces), max_chars)


def _read_pdf_entry(
    entry,
    entry_id: int,
    raw: bytes,
    max_preview_chars: int,
    deadline: float | None = None,
) -> EntryRecord | None:
    excerpt = extract_pdf_excerpt(raw, max_preview_chars, deadline=deadline)
    if len(excerpt) < MIN_BLOCK_CHARS:
        # No text layer (a scan), password-protected, or malformed: skip it
        # rather than store a title-only entry with no searchable body.
        return None
    return EntryRecord(entry_id, entry.title, excerpt, entry.path, None)


def _read_plain_text_entry(
    entry,
    entry_id: int,
    raw: bytes,
    max_preview_chars: int,
) -> EntryRecord | None:
    # Bundled licenses, dotfiles and configs are text/plain too; only real
    # article text should become a search result.
    if _PLAIN_TEXT_ASSET.search(entry.path) or _PLAIN_TEXT_ASSET.search(entry.title):
        return None
    excerpt = _plain_text_excerpt(raw, max_preview_chars)
    # Require real prose: a two-byte .gitignore or a stray asset is not an
    # article, but a text ZIM entry is.
    if len(excerpt) < MIN_BLOCK_CHARS or is_javascript_shell(raw, excerpt):
        return None
    title = (
        disambiguation_title(entry.title)
        if is_disambiguation(entry.title, excerpt, raw)
        else entry.title
    )
    return EntryRecord(entry_id, title, excerpt, entry.path, None)


def _read_html_entry(
    zim: Archive,
    entry,
    entry_id: int,
    html: bytes,
    max_preview_chars: int,
    deadline: float | None = None,
) -> EntryRecord | None:
    if _HTML_ASSET_PATH.search(entry.path) and not _HTML_TAG.search(html):
        # A script/data file served as HTML (zimit crawls do this for <site>.js)
        # has no markup. A real page that merely ends in .js — a cheatsheet
        # about Commander.js — does contain HTML tags, so keep it.
        return None
    refresh_url = _refresh_url(html)

    if refresh_url:
        url = unquote(refresh_url)
        base, _, fragment = url.partition("#")
        path = posixpath.normpath(posixpath.join(posixpath.dirname(entry.path), base))
        if not zim.has_entry_by_path(path):
            return None
        companion = _companion(zim, entry, fragment) if fragment else None
        if companion is not None:
            kind, body = companion
            if kind == "markdown":
                excerpt, md_title = _markdown_excerpt(
                    body, max_preview_chars, deadline=deadline
                )
                if excerpt and not is_javascript_shell(body, excerpt):
                    return EntryRecord(
                        entry_id,
                        entry.title or md_title,
                        excerpt,
                        entry.path,
                        None,
                    )
            else:
                # Keep this page's own title and path: it is a deep link into the
                # app route, and its body is real text. Storing the shell target
                # instead would collapse every article onto "index.html".
                excerpt = extract_excerpt(
                    body, max_preview_chars=max_preview_chars, deadline=deadline
                )
                if not excerpt and not _HTML_TAG.search(body):
                    # Plain-text companions (video/playlist descriptions) have no
                    # HTML blocks, so normalize them directly. An HTML body with no
                    # visible text stays empty instead of leaking its markup.
                    excerpt = _plain_text_excerpt(body, max_preview_chars)
                if excerpt and not is_javascript_shell(body, excerpt):
                    title = (
                        disambiguation_title(entry.title)
                        if is_disambiguation(entry.title, excerpt, body)
                        else entry.title
                    )
                    return EntryRecord(
                        entry_id,
                        title,
                        excerpt,
                        entry.path,
                        None,
                    )
        target = zim.get_entry_by_path(path)
        return EntryRecord(
            entry_id,
            entry.title,
            "",
            target.path,
            target._index,
        )

    excerpt = extract_excerpt(html, max_preview_chars=max_preview_chars, deadline=deadline)
    if is_javascript_shell(html, excerpt):
        # An app shell with no article text: nothing useful to index.
        return None
    if not excerpt:
        # No visible blocks. An app shell has nothing to index even if its head
        # carries a title/description; otherwise a meta description is better
        # than a title-only entry.
        if any(marker in html for marker in JS_SHELL_MARKERS):
            return None
        if _is_loader_shell(html):
            # A JS app that renders content it does not ship: keep the page as
            # title-only, without a shared meta description that would collapse
            # every article onto the shell.
            return EntryRecord(entry_id, entry.title, "", entry.path, None)
        excerpt = truncate_at_word_boundary(_meta_description(html), max_preview_chars)
    title = (
        disambiguation_title(entry.title)
        if is_disambiguation(entry.title, excerpt, html)
        else entry.title
    )
    return EntryRecord(entry_id, title, excerpt, entry.path, None)


def read_entry(
    zim: EntrySource,
    i: int,
    fast: bool = False,
    max_html_bytes: int = DEFAULT_MAX_HTML_BYTES,
    max_entry_bytes: int = DEFAULT_MAX_ENTRY_BYTES,
    max_preview_chars: int = DEFAULT_PREVIEW_CHARS,
    timeout: float | None = None,
) -> EntryRecord | None:
    """Return (id, title, excerpt, path, target_id), or None.

    Redirects (real ones, and small meta refresh pages) get empty excerpts and the
    path and id of the page they point to, so they are searchable by title only.

    fast=True stores the title and path without reading the article body, so no
    text (and therefore no vector) is produced.

    ``timeout`` bounds the whole parse (HTML and PDF) cooperatively via a
    shared ``time.monotonic()`` deadline; when it passes, the entry yields a
    partial excerpt rather than hanging the caller. The builder passes no
    timeout so nothing is silently dropped offline.

    """
    entry = zim._get_entry_by_id(i)
    if entry.is_redirect:
        target = entry.get_redirect_entry()
        return EntryRecord(i, entry.title, "", target.path, target._index)

    item = entry.get_item()
    mimetype = item.mimetype.split(";", 1)[0].strip().lower()
    if mimetype not in _READABLE_MIMETYPES:
        return None

    if fast:
        return EntryRecord(i, entry.title, "", entry.path, None)

    try:
        entry_limit = max(1, int(max_entry_bytes))
    except (TypeError, ValueError):
        entry_limit = DEFAULT_MAX_ENTRY_BYTES
    entry_size = int(getattr(item, "size", 0) or 0)
    if entry_size > entry_limit:
        return None

    deadline = (
        time.monotonic() + float(timeout) if timeout is not None else None
    )

    content = item.content
    try:
        html_limit = max(1, int(max_html_bytes))
    except (TypeError, ValueError):
        html_limit = DEFAULT_MAX_HTML_BYTES
    # A PDF cannot be parsed from a prefix (its cross-reference table sits at the
    # end), and libzim has already decompressed the whole entry, so read it whole.
    raw = bytes(content) if mimetype == "application/pdf" else bytes(content[:html_limit])
    del content, item

    if mimetype == "application/pdf":
        return _read_pdf_entry(entry, i, raw, max_preview_chars, deadline=deadline)
    if mimetype == "text/plain":
        return _read_plain_text_entry(entry, i, raw, max_preview_chars)
    return _read_html_entry(
        cast(Archive, zim), entry, i, raw, max_preview_chars, deadline=deadline
    )

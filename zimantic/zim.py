import codecs
import json
import posixpath
import re
from html.parser import HTMLParser
from urllib.parse import unquote
from libzim.reader import Archive, set_cluster_cache_max_size

# libzim undercounts this cache: its 16 MB default really used ~170 MB. 1 MB used ~4 MB and wasn't slower.
set_cluster_cache_max_size(1 << 20) # 1*2^20 = ~1MB

MIN_LEAD = 50    # shorter first paragraphs? -> page searchable by title only
MAX_LEAD = 1000  # characters kept for display; the model reads at most 256 tokens anyway
DEFAULT_MAX_HTML_BYTES = 4 << 20
MAX_HTML_BYTES = DEFAULT_MAX_HTML_BYTES  # compatibility alias for the default extraction limit
REFRESH_SCAN_BYTES = 64 << 10
REFRESH_CONTENT = re.compile(r"^\s*0\s*;\s*url\s*=\s*(.*?)\s*$", re.I)

# Disambiguation pages are stored with kind=DISAMBIGUATION plus the entries they
# link to, so search can cluster the hub with its members without re-reading the
# ZIM. Detection is MediaWiki-flavoured: the "(disambiguation)" title suffix is
# the reliable signal, and an unsuffixed hub declares itself with a "may refer
# to" lead. The marker is matched only near the start of the visible text; that
# keeps prose pages and navboxes that merely mention the phrase out of the hub
# set, at the cost of the occasional template-less hub.
DISAMBIGUATION = "disambiguation"
DISAMBIG_TITLE = re.compile(r"\s*\(disambiguation\)\s*$", re.I)
MAY_REFER = re.compile(r"\bmay refer to\b", re.I)
DISAMBIG_MARKER_WINDOW = 200
MAX_DISAMBIG_MEMBERS = 50
# Namespaces whose links are not article targets (casefolded, no trailing "_").
_NON_ARTICLE_PREFIXES = {
    "category", "file", "image", "help", "mediawiki", "portal", "special",
    "talk", "template", "user", "wikipedia", "wiktionary", "wikiquote",
    "wikisource", "module", "draft", "book", "timedtext",
}

class _Found(Exception): # stop feed() early
    pass


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
    """Collect either the first substantial paragraph or all visible page text.

    How it runs: we never call the handle_* methods ourselves. HTMLParser.feed(html) reads the
    HTML left to right and calls them as it goes, in whatever order the HTML has:
        <p>         -> handle_starttag("p", attrs)
        some text   -> handle_data("some text")
        </p>        -> handle_endtag("p")
    HTMLParser's own versions do nothing; this class overrides them to collect the first paragraph.
    """

    BLOCK_TAGS = {
        "address", "article", "aside", "blockquote", "br", "dd", "div", "dl",
        "dt", "figcaption", "figure", "footer", "h1", "h2", "h3", "h4", "h5",
        "h6", "header", "hr", "li", "main", "nav", "ol", "p", "pre", "section",
        "table", "td", "th", "tr", "ul",
    }
    SKIP_TAGS = {"head", "script", "style", "template"}

    def __init__(self, first_paragraph: bool):
        super().__init__()
        self.first_paragraph = first_paragraph
        self.in_p = False
        self.skipping = []  # open tags whose contents are not article text

        # Text arrives in pieces, e.g. ["A ", "black hole", " is a region of ", "spacetime", "…"].
        self.text = []
        self.parts = []
        self.result = ""

    # Called at every opening tag.
    def handle_starttag(self, tag, attrs):
        if tag in self.SKIP_TAGS or (
            tag == "sup" and "reference" in (dict(attrs).get("class") or "")
        ):
            self.skipping.append(tag)
            return

        if self.first_paragraph and tag == "p":
            self.in_p = True
            self.text = []
        elif not self.first_paragraph and tag in self.BLOCK_TAGS:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if self.skipping and self.skipping[-1] == tag:  # the <style>/<script>/<sup> we were skipping ended
            self.skipping.pop()
        elif self.first_paragraph and tag == "p" and self.in_p:
            self.in_p = False
            text = " ".join("".join(self.text).split())  # glue the pieces, collapse newlines/extra spaces
            if len(text) >= MIN_LEAD:
                self.result = text[:MAX_LEAD]
                raise _Found  # done: skip the rest of the article
            # too short ("Mercury may refer to:"): drop it and wait for the next <p>
        elif not self.first_paragraph and tag in self.BLOCK_TAGS:
            self.parts.append("\n")

    def handle_data(self, data):
        if self.skipping:
            return
        if self.first_paragraph and self.in_p:
            self.text.append(data)
        elif not self.first_paragraph:
            self.parts.append(data)


class _LinkExtractor(HTMLParser):
    """Collect (href, visible text) for every <a> in a page."""

    SKIP_TAGS = _TextExtractor.SKIP_TAGS

    def __init__(self):
        super().__init__()
        self.skipping = []
        self.links = []
        self._href = None
        self._text = []

    def handle_starttag(self, tag, attrs):
        if tag in self.SKIP_TAGS:
            self.skipping.append(tag)
            return
        if self.skipping:
            return
        if tag == "a":
            self._href = dict(attrs).get("href")
            self._text = []

    def handle_endtag(self, tag):
        if self.skipping and self.skipping[-1] == tag:
            self.skipping.pop()
            return
        if self.skipping:
            return
        if tag == "a" and self._href is not None:
            self.links.append((self._href, " ".join("".join(self._text).split())))
            self._href = None
            self._text = []

    def handle_data(self, data):
        if self.skipping:
            return
        if self._href is not None:
            self._text.append(data)


def is_disambiguation(title: str, text: str) -> bool:
    """True for a MediaWiki disambiguation page (title suffix or early marker)."""
    if DISAMBIG_TITLE.search(title):
        return True
    marker = MAY_REFER.search(text)
    return marker is not None and marker.start() <= DISAMBIG_MARKER_WINDOW


def disambiguation_members(html: bytes, path: str, limit: int = MAX_DISAMBIG_MEMBERS) -> list[dict]:
    """Article links on a disambiguation page, resolved against its own path."""
    parser = _LinkExtractor()
    parser.feed(html.decode("utf-8", "ignore"))

    directory = posixpath.dirname(path)
    seen: set[str] = set()
    members: list[dict] = []
    for href, text in parser.links:
        href = (href or "").strip()
        if not href or href.startswith(("#", "//", "http:", "https:", "mailto:")):
            continue
        target = posixpath.normpath(posixpath.join(directory, unquote(href.split("#", 1)[0])))
        if target == path or target.startswith("../") or target in seen:
            continue
        if any(
            part.split(":", 1)[0].replace("_", " ").casefold() in _NON_ARTICLE_PREFIXES
            for part in target.split("/")
            if ":" in part
        ):
            continue
        seen.add(target)
        members.append({
            "title": text or target.rsplit("/", 1)[-1].replace("_", " "),
            "path": target,
        })
        if len(members) >= limit:
            break
    return members


CHUNK = 65536  # bytes of HTML per feed() call (64 KB); most leads are found in the first chunk


def _extract_text(html: bytes, first_paragraph: bool) -> str:
    parser = _TextExtractor(first_paragraph)

    # "ignore" drops bytes that aren't valid UTF-8 rather than crashing.
    decoder = codecs.getincrementaldecoder("utf-8")("ignore")
    try:
        for start in range(0, len(html), CHUNK):
            parser.feed(decoder.decode(html[start:start + CHUNK]))  # feed() calls the handle_* methods
        parser.feed(decoder.decode(b"", final=True))
    except _Found:
        pass  
    if first_paragraph:
        return parser.result
    return " ".join("".join(parser.parts).split())


def first_paragraph(html: bytes) -> str: # "" if no paragraph was long enough
    return _extract_text(html, first_paragraph=True)


def full_text(html: bytes) -> str:
    return _extract_text(html, first_paragraph=False)


def read_entry(
    zim: Archive,
    i: int,
    fast: bool = False,
    first_paragraph: bool = False,
    max_html_bytes: int = DEFAULT_MAX_HTML_BYTES,
):
    """Return (id, title, lead, path, target_id, members) for an HTML page, or None.

    Redirects (real ones, and small meta refresh pages) get lead "" and the
    path and id of the page they point to, so they are searchable by title only.

    fast=True stores the title and path without reading the article body, so no
    text (and therefore no vector) is produced. Otherwise, first_paragraph
    selects between the first substantial paragraph and the whole page text.

    members is a JSON array of {title, path} for a disambiguation page (so
    search can cluster the hub with its entries), or None for ordinary pages.
    """
    entry = zim._get_entry_by_id(i)
    if entry.is_redirect:
        target = entry.get_redirect_entry()
        return i, entry.title, "", target.path, target._index, None
    
    item = entry.get_item()
    if not item.mimetype.startswith("text/html"): 
        return None

    if fast:
        return i, entry.title, "", entry.path, None, None

    content = item.content
    try:
        html_limit = max(1, int(max_html_bytes))
    except (TypeError, ValueError):
        html_limit = DEFAULT_MAX_HTML_BYTES
    html = bytes(content[:html_limit])
    del content, item
    refresh_url = _refresh_url(html)
    
    if refresh_url:
        url = unquote(refresh_url).split("#", 1)[0]
        path = posixpath.normpath(posixpath.join(posixpath.dirname(entry.path), url))
        if not zim.has_entry_by_path(path):
            return None
        target = zim.get_entry_by_path(path)
        return i, entry.title, "", target.path, target._index, None
    text = _extract_text(html, first_paragraph)
    members = None
    if is_disambiguation(entry.title, text):
        members = json.dumps(disambiguation_members(html, entry.path))
    return i, entry.title, text, entry.path, None, members

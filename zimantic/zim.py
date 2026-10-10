import codecs
import json
import posixpath
import re
from html.parser import HTMLParser
from urllib.parse import unquote
from typing import Iterator
from libzim.reader import Archive, set_cluster_cache_max_size

# libzim undercounts this cache: its 16 MB default really used ~170 MB. 1 MB used ~4 MB and wasn't slower.
set_cluster_cache_max_size(1 << 20) # 1*2^20 = ~1MB

MIN_BLOCK_CHARS = 50    # shorter blocks are ignored; the page remains searchable by title
DEFAULT_PREVIEW_CHARS = 1000
DEFAULT_MAX_HTML_BYTES = 4 << 20
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
    r"|css|scss|less|xml|svg|png|jpe?g|gif|webp|ico|csv|tsv|log|lock))"
    r"(?:[?#][^/]*)?$"
    # License/notice files, which are conventionally ALL CAPS.
    r"|(^|/)(?:COPYING|LICEN[CS]E|PATENTS?|NOTICE|README|CHANGELOG|AUTHORS"
    r"|CONTRIBUTORS|MAKEFILE|DOCKERFILE)(?:[._-]|$)"
    # Anything inside a bundle's asset directory.
    r"|(?i:(^|/)(?:assets?|static|_static|_assets|media|vendor|node_modules"
    r"|mathjax|fonts?|images?|img|css|js|scripts?|downloads?)/)"
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
JS_SHELL_MARKERS = (b"<noscript", b'id="app"', b"id='app'")
JS_NOTICE_MAX_CHARS = 300

# Page chrome that is navigation, maintenance or boilerplate rather than article
# text. Matching these by id, class token or ARIA role keeps category footers,
# navboxes and tables of contents out of excerpts (and therefore out of the
# embedding), which is a large share of the text on MediaWiki stubs.
SKIP_IDS = frozenset({
    "catlinks", "mw-hidden-catlinks", "printfooter", "footer",
    "toc", "mw-navigation", "sitefooter",
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
    # LibreTexts/MindTouch app-shell topic listings.
    "mt-topic-hierarchy-listings", "mt-guide-listings", "mt-listing-detailed",
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

    @staticmethod
    def _is_chrome(tag, attributes) -> bool:
        """True for navigation, maintenance or boilerplate containers."""
        if tag in _TextExtractor.SKIP_TAGS or tag in _TextExtractor.CHROME_TAGS:
            return True
        if tag == "sup" and "reference" in (attributes.get("class") or ""):
            return True
        if (attributes.get("aria-hidden") or "").casefold() == "true":
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
            block["parts"].append(" ")

    def handle_starttag(self, tag, attrs):
        if self.skipping:
            return
        attributes = dict(attrs)
        if self._is_chrome(tag, attributes):
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
                "parts": [],
            }
            self.blocks.append(block)
            self.active.append(block)
            self.block_order += 1

    def handle_endtag(self, tag):
        if self.skipping and self.skipping[-1] == tag:  # the <style>/<script>/<sup> we were skipping ended
            self.skipping.pop()
        elif tag in self.CANDIDATE_PRIORITIES:
            for index in range(len(self.active) - 1, -1, -1):
                if self.active[index]["tag"] == tag:
                    block = self.active.pop(index)
                    text = self._text(block["parts"])
                    block["text"] = text
                    if len(text) >= MIN_BLOCK_CHARS:
                        for parent in self.active[:index]:
                            parent["nested"] = True
                    break
        if not self.skipping and tag in self.SEPARATOR_TAGS and self.active:
            self._separate()

    def handle_data(self, data):
        if self.skipping:
            return
        for block in self.active:
            block["parts"].append(data)

    @staticmethod
    def _text(parts):
        text = BOILERPLATE.sub(" ", "".join(parts))
        return " ".join(text.split())

    def candidates(self) -> list[str]:
        candidates = []
        for block in self.blocks:
            if block["nested"]:
                continue
            text = block.get("text", self._text(block["parts"]))
            if len(text) >= MIN_BLOCK_CHARS:
                candidates.append((block["priority"], block["order"], text))
        return [
            text
            for _, _, text in sorted(candidates, key=lambda candidate: candidate[:2])
    ]


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


CHUNK = 65536  # bytes of HTML per feed() call (64 KB)


def iter_text_blocks(html: bytes) -> Iterator[str]:
    """Yield substantial article blocks, preferring paragraphs over fallbacks."""
    parser = _TextExtractor()

    # "ignore" drops bytes that aren't valid UTF-8 rather than crashing.
    decoder = codecs.getincrementaldecoder("utf-8")("ignore")
    for start in range(0, len(html), CHUNK):
        parser.feed(decoder.decode(html[start:start + CHUNK]))
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

    One excerpt serves both the preview and the embedding. The model truncates
    it to ``embedding_tokens`` when it builds the vector, so there is no need to
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
) -> str:
    """Return the article's opening text, bounded to ``max_preview_chars``.

    The same stored excerpt is the UI preview and the embedding input; the
    embedder truncates it to ``embedding_tokens`` when it creates the vector.
    """
    max_chars = max(1, int(max_preview_chars))
    candidates = list(iter_text_blocks(html))
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


def _spa_body(zim: Archive, entry, fragment: str) -> bytes | None:
    """Companion text for an app-shell stub, or None.

    Two conventions are supported: Kiwix's ``content/page_content_<id>.json``
    keyed by the numeric page id, and app bundles that keep a ``<slug>.json``
    companion for the deep link, commonly under ``videos/`` or ``playlists/``.
    The result may be HTML or plain text; the caller decides how to read it.
    """
    match = SPA_PAGE_ID.search(entry.path)
    if match:
        body = _json_body(zim, SPA_CONTENT_PATH.format(id=match.group(1)))
        if body is not None:
            return body
    slug = _fragment_slug(fragment)
    if not slug:
        return None
    for directory in SPA_COMPANION_DIRS:
        candidate = posixpath.join(directory, slug + ".json") if directory else slug + ".json"
        body = _json_body(zim, candidate)
        if body is not None:
            return body
    # Some bundles keep the JSON beside the stub entry.
    return _json_body(zim, posixpath.join(posixpath.dirname(entry.path), slug + ".json"))


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


def _plain_text_excerpt(raw: bytes, max_chars: int) -> str:
    """Turn a non-HTML text entry into a single normalized excerpt."""
    text = raw.decode("utf-8", "ignore")
    return truncate_at_word_boundary(" ".join(text.split()), max_chars)


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
) -> str:
    """Return a PDF's opening text, bounded to ``max_preview_chars``.

    Only whole pages are read, and only until the character budget is filled, so
    a long document costs little. A scanned PDF has no text layer and yields an
    empty string, which the caller treats as "not indexable" rather than storing
    a title-only or boilerplate entry. A password-protected or malformed PDF is
    skipped the same way instead of failing the build.
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


def read_entry(
    zim: Archive,
    i: int,
    fast: bool = False,
    max_html_bytes: int = DEFAULT_MAX_HTML_BYTES,
    max_preview_chars: int = DEFAULT_PREVIEW_CHARS,
):
    """Return (id, title, excerpt, path, target_id), or None.

    Redirects (real ones, and small meta refresh pages) get empty excerpts and the
    path and id of the page they point to, so they are searchable by title only.

    fast=True stores the title and path without reading the article body, so no
    text (and therefore no vector) is produced.

    """
    entry = zim._get_entry_by_id(i)
    if entry.is_redirect:
        target = entry.get_redirect_entry()
        return i, entry.title, "", target.path, target._index

    item = entry.get_item()
    mimetype = item.mimetype.split(";", 1)[0].strip().lower()
    if mimetype not in _READABLE_MIMETYPES:
        return None

    if fast:
        return i, entry.title, "", entry.path, None

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
        excerpt = extract_pdf_excerpt(raw, max_preview_chars)
        if len(excerpt) < MIN_BLOCK_CHARS:
            # No text layer (a scan), password-protected, or malformed: skip it
            # rather than store a title-only entry with no searchable body.
            return None
        return i, entry.title, excerpt, entry.path, None

    if mimetype == "text/plain":
        # Bundled licenses, dotfiles and configs are text/plain too; only real
        # article text should become a search result.
        if _PLAIN_TEXT_ASSET.search(entry.path) or _PLAIN_TEXT_ASSET.search(entry.title):
            return None
        excerpt = _plain_text_excerpt(raw, max_preview_chars)
        # Require real prose: a two-byte .gitignore or a stray asset is not an
        # article, but a text ZIM entry is.
        if len(excerpt) < MIN_BLOCK_CHARS or is_javascript_shell(raw, excerpt):
            return None
        title = disambiguation_title(entry.title) if is_disambiguation(entry.title, excerpt, raw) else entry.title
        return i, title, excerpt, entry.path, None

    html = raw
    refresh_url = _refresh_url(html)

    if refresh_url:
        url = unquote(refresh_url)
        base, _, fragment = url.partition("#")
        path = posixpath.normpath(posixpath.join(posixpath.dirname(entry.path), base))
        if not zim.has_entry_by_path(path):
            return None
        body = _spa_body(zim, entry, fragment) if fragment else None
        if body is not None:
            # Keep this page's own title and path: it is a deep link into the
            # app route, and its body is real text. Storing the shell target
            # instead would collapse every article onto "index.html".
            excerpt = extract_excerpt(body, max_preview_chars=max_preview_chars)
            if not excerpt and not _HTML_TAG.search(body):
                # Plain-text companions (video/playlist descriptions) have no
                # HTML blocks, so normalize them directly. An HTML body with no
                # visible text stays empty instead of leaking its markup.
                excerpt = _plain_text_excerpt(body, max_preview_chars)
            if excerpt and not is_javascript_shell(body, excerpt):
                title = disambiguation_title(entry.title) if is_disambiguation(
                    entry.title, excerpt, body
                ) else entry.title
                return i, title, excerpt, entry.path, None
        target = zim.get_entry_by_path(path)
        return i, entry.title, "", target.path, target._index

    excerpt = extract_excerpt(html, max_preview_chars=max_preview_chars)
    if is_javascript_shell(html, excerpt):
        # An app shell with no article text: nothing useful to index.
        return None
    if not excerpt:
        # No visible blocks. An app shell has nothing to index even if its head
        # carries a title/description; otherwise a meta description is better
        # than a title-only entry.
        if any(marker in html for marker in JS_SHELL_MARKERS):
            return None
        excerpt = truncate_at_word_boundary(_meta_description(html), max_preview_chars)
    title = disambiguation_title(entry.title) if is_disambiguation(entry.title, excerpt, html) else entry.title
    return i, title, excerpt, entry.path, None

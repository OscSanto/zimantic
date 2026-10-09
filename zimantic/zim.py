import codecs
import json
import posixpath
import re
from html.parser import HTMLParser
from urllib.parse import unquote
from typing import Callable, Iterator
from libzim.reader import Archive, set_cluster_cache_max_size
from .settings import DEFAULT_MAX_EMBEDDING_TOKENS

# libzim undercounts this cache: its 16 MB default really used ~170 MB. 1 MB used ~4 MB and wasn't slower.
set_cluster_cache_max_size(1 << 20) # 1*2^20 = ~1MB

MIN_BLOCK_CHARS = 50    # shorter blocks are ignored; the page remains searchable by title
DEFAULT_PREVIEW_CHARS = 1000
DEFAULT_MAX_HTML_BYTES = 4 << 20
DEFAULT_PREVIEW_OVERFLOW = "skip"
DEFAULT_EMBEDDING_OVERFLOW = "truncate"
OVERFLOW_POLICIES = {"skip", "truncate"}
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
JS_SHELL_MARKERS = (b"<noscript", b'id="app"', b"id='app'")
JS_NOTICE_MAX_CHARS = 300

# Disambiguation pages are persisted with a title suffix, so search can apply
# the same ranking policy without a separate metadata table.
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

    def __init__(self):
        super().__init__()
        self.skipping = []  # open tags whose contents are not article text
        self.blocks = []
        self.active = []
        self.block_order = 0

    def handle_starttag(self, tag, attrs):
        if self.skipping:
            return
        attributes = dict(attrs)
        classes = (attributes.get("class") or "").casefold().split()
        is_footer = tag == "div" and any(
            class_name.endswith("footer") for class_name in classes
        )
        if tag in self.SKIP_TAGS or is_footer or tag in self.CHROME_TAGS or (
            tag == "sup" and "reference" in (attributes.get("class") or "")
        ):
            self.skipping.append(tag)
            return

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
                    for parent in self.active[:index]:
                        parent["parts"].append(" ")
                    break

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


def _policy(value: str, default: str) -> str:
    policy = default if value is None else str(value).casefold()
    if policy not in OVERFLOW_POLICIES:
        choices = ", ".join(sorted(OVERFLOW_POLICIES))
        raise ValueError(f"overflow policy must be one of {choices}, got {value!r}")
    return policy


def truncate_at_word_boundary(text: str, max_chars: int) -> str:
    """Limit text without cutting through a word when a boundary is available."""
    limit = max(1, int(max_chars))
    if len(text) <= limit:
        return text
    cut = text[:limit]
    boundary = max(cut.rfind(" "), cut.rfind("\n"), cut.rfind("\t"))
    return cut[:boundary].rstrip() if boundary > 0 else ""


def _preview_excerpt(
    candidates: list[str],
    max_chars: int,
    overflow: str,
) -> str:
    accepted: list[str] = []
    for candidate in candidates:
        separator = "\n\n" if accepted else ""
        available = max_chars - len(separator) - sum(map(len, accepted)) - max(0, len(accepted) - 1) * 2
        if len(candidate) <= available:
            accepted.append(candidate)
        elif overflow == "truncate" and available > 0:
            fitted = truncate_at_word_boundary(candidate, available)
            if fitted:
                accepted.append(fitted)
            break
    if accepted:
        return "\n\n".join(accepted)
    return truncate_at_word_boundary(candidates[0], max_chars) if candidates else ""


def _embedding_excerpt(
    candidates: list[str],
    title: str,
    max_tokens: int,
    overflow: str,
    token_count: Callable[[str, str], int] | None,
    truncate: Callable[[str, str], str] | None,
) -> str:
    if not candidates or token_count is None or truncate is None:
        return ""
    prefix = f"passage: {title}\n"
    accepted: list[str] = []
    for candidate in candidates:
        separator = "\n\n" if accepted else ""
        current = "\n\n".join(accepted)
        proposed = current + separator + candidate
        if token_count(proposed, prefix) <= max_tokens:
            accepted.append(candidate)
            continue
        if overflow == "truncate":
            fitted = truncate(candidate, prefix=prefix + current + separator)
            if fitted:
                accepted.append(fitted)
            break
    if accepted:
        return "\n\n".join(accepted)
    return truncate(candidates[0], prefix=prefix)


def extract_excerpt(
    html: bytes,
    title: str = "",
    max_preview_chars: int = DEFAULT_PREVIEW_CHARS,
    max_embedding_tokens: int = DEFAULT_MAX_EMBEDDING_TOKENS,
    preview_overflow: str = DEFAULT_PREVIEW_OVERFLOW,
    embedding_overflow: str = DEFAULT_EMBEDDING_OVERFLOW,
    embedding_token_count: Callable[[str, str], int] | None = None,
    embedding_truncate: Callable[[str, str], str] | None = None,
) -> str:
    """Return one stored excerpt large enough for preview or embedding use."""
    max_chars = max(1, int(max_preview_chars))
    max_tokens = max(1, int(max_embedding_tokens))
    candidates = list(iter_text_blocks(html))
    preview = _preview_excerpt(
        candidates,
        max_chars,
        _policy(preview_overflow, DEFAULT_PREVIEW_OVERFLOW),
    )
    embedding = _embedding_excerpt(
        candidates,
        title,
        max_tokens,
        _policy(embedding_overflow, DEFAULT_EMBEDDING_OVERFLOW),
        embedding_token_count,
        embedding_truncate,
    )
    return max(
        (preview, embedding),
        key=len,
        default="",
    )


def _spa_page_body(zim: Archive, entry) -> bytes | None:
    """Article HTML for a page rendered by a JavaScript app shell, if any.

    Returns the ``htmlBody`` from the companion content JSON
    (``content/page_content_<id>.json``) when the entry follows that convention,
    or None for ordinary ZIMs.
    """
    match = SPA_PAGE_ID.search(entry.path)
    if not match:
        return None
    content_path = SPA_CONTENT_PATH.format(id=match.group(1))
    if not zim.has_entry_by_path(content_path):
        return None
    try:
        payload = json.loads(bytes(zim.get_entry_by_path(content_path).get_item().content))
    except (LookupError, RuntimeError, TypeError, ValueError):
        return None
    body = payload.get("htmlBody") if isinstance(payload, dict) else None
    return body.encode("utf-8") if isinstance(body, str) else None


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


def read_entry(
    zim: Archive,
    i: int,
    fast: bool = False,
    max_html_bytes: int = DEFAULT_MAX_HTML_BYTES,
    max_preview_chars: int = DEFAULT_PREVIEW_CHARS,
    max_embedding_tokens: int = DEFAULT_MAX_EMBEDDING_TOKENS,
    preview_overflow: str = DEFAULT_PREVIEW_OVERFLOW,
    embedding_overflow: str = DEFAULT_EMBEDDING_OVERFLOW,
    embedder=None,
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
    if not item.mimetype.startswith("text/html"): 
        return None

    if fast:
        return i, entry.title, "", entry.path, None

    content = item.content
    try:
        html_limit = max(1, int(max_html_bytes))
    except (TypeError, ValueError):
        html_limit = DEFAULT_MAX_HTML_BYTES
    html = bytes(content[:html_limit])
    del content, item
    refresh_url = _refresh_url(html)
    
    if refresh_url:
        url = unquote(refresh_url)
        base, _, fragment = url.partition("#")
        path = posixpath.normpath(posixpath.join(posixpath.dirname(entry.path), base))
        if not zim.has_entry_by_path(path):
            return None
        body = _spa_page_body(zim, entry) if fragment else None
        if body is not None:
            # Keep this page's own title and path: it is a deep link into the
            # app route, and its body is real text. Storing the shell target
            # instead would collapse every article onto "index.html".
            excerpt = extract_excerpt(
                body,
                title=entry.title,
                max_preview_chars=max_preview_chars,
                max_embedding_tokens=max_embedding_tokens,
                preview_overflow=preview_overflow,
                embedding_overflow=embedding_overflow,
                embedding_token_count=getattr(embedder, "token_count", None),
                embedding_truncate=getattr(embedder, "truncate", None),
            )
            if excerpt and not is_javascript_shell(body, excerpt):
                title = disambiguation_title(entry.title) if is_disambiguation(
                    entry.title, excerpt, body
                ) else entry.title
                return i, title, excerpt, entry.path, None
        target = zim.get_entry_by_path(path)
        return i, entry.title, "", target.path, target._index
    excerpt = extract_excerpt(
        html,
        title=entry.title,
        max_preview_chars=max_preview_chars,
        max_embedding_tokens=max_embedding_tokens,
        preview_overflow=preview_overflow,
        embedding_overflow=embedding_overflow,
        embedding_token_count=getattr(embedder, "token_count", None),
        embedding_truncate=getattr(embedder, "truncate", None),
    )
    if is_javascript_shell(html, excerpt):
        # An app shell with no article text: nothing useful to index.
        return None
    title = disambiguation_title(entry.title) if is_disambiguation(entry.title, excerpt, html) else entry.title
    return i, title, excerpt, entry.path, None

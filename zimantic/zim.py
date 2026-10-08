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
                    text = " ".join("".join(block["parts"]).split())
                    block["text"] = text
                    if len(text) >= MIN_BLOCK_CHARS:
                        for parent in self.active[:index]:
                            parent["nested"] = True
                    break

    def handle_data(self, data):
        if self.skipping:
            return
        for block in self.active:
            block["parts"].append(data)

    def candidates(self) -> list[str]:
        candidates = []
        for block in self.blocks:
            if block["nested"]:
                continue
            text = block.get("text", " ".join("".join(block["parts"]).split()))
            if len(text) >= MIN_BLOCK_CHARS:
                candidates.append((block["priority"], block["order"], text))
        return [
            text
            for _, _, text in sorted(candidates, key=lambda candidate: candidate[:2])
    ]


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
    """Return (id, title, excerpt, path, target_id, members), or None.

    Redirects (real ones, and small meta refresh pages) get empty excerpts and the
    path and id of the page they point to, so they are searchable by title only.

    fast=True stores the title and path without reading the article body, so no
    text (and therefore no vector) is produced.

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
    members = None
    if is_disambiguation(entry.title, excerpt):
        members = json.dumps(disambiguation_members(html, entry.path))
    return i, entry.title, excerpt, entry.path, None, members

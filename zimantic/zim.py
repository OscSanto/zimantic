import codecs
import posixpath
import re
from html.parser import HTMLParser
from urllib.parse import unquote
from libzim.reader import Archive, set_cluster_cache_max_size

# libzim undercounts this cache: its 16 MB default really used ~170 MB. 1 MB used ~4 MB and wasn't slower.
set_cluster_cache_max_size(1 << 20) # 1*2^20 = ~1MB

MIN_LEAD = 50    # shorter first paragraphs? -> page searchable by title only
MAX_LEAD = 1000  # characters kept for display; the model reads at most 256 tokens anyway
MAX_HTML_BYTES = 1 << 20  # cap extraction work and temporary memory for unusually large pages
REFRESH_SCAN_BYTES = 64 << 10
REFRESH_CONTENT = re.compile(r"^\s*0\s*;\s*url\s*=\s*(.*?)\s*$", re.I)

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


class _FirstParagraph(HTMLParser):
    """Collects the text of the first <p> that is long enough, skipping styles and [1] footnotes.

    How it runs: we never call the handle_* methods ourselves. HTMLParser.feed(html) reads the
    HTML left to right and calls them as it goes, in whatever order the HTML has:
        <p>         -> handle_starttag("p", attrs)
        some text   -> handle_data("some text")
        </p>        -> handle_endtag("p")
    HTMLParser's own versions do nothing; this class overrides them to collect the first paragraph.
    """

    # The handle_* methods are called by feed() in the order the HTML has.
    def __init__(self):
        super().__init__()
        self.in_p = False
        self.skipping = []  # open <style>, <script> or footnote <sup> tags

        # Scratch buffer for the <p> being read. 
        # Text arrives in pieces, e.g. ["A ", "black hole", " is a region of ", "spacetime", "…"].
        # Emptied at every <p>; joined at </p>.
        self.text = []

        self.lead = ""  # final answer

    # Called at every opening tag.
    def handle_starttag(self, tag, attrs):
        if tag == "p":  # a new paragraph starts: collect it from scratch
            self.in_p=True 
            self.text = []

        # keep junk inside <p> out
        elif tag in ("style", "script") or (tag == "sup" and "reference" in (dict(attrs).get("class") or "")):
            self.skipping.append(tag)

    def handle_endtag(self, tag):
        if self.skipping and self.skipping[-1] == tag:  # the <style>/<script>/<sup> we were skipping ended
            self.skipping.pop()
        elif tag == "p" and self.in_p:
            self.in_p = False
            text = " ".join("".join(self.text).split())  # glue the pieces, collapse newlines/extra spaces
            if len(text) >= MIN_LEAD:
                self.lead = text[:MAX_LEAD]
                raise _Found  # done: skip the rest of the article
            # too short ("Mercury may refer to:"): drop it and wait for the next <p>

    def handle_data(self, data):
        if self.in_p and not self.skipping:
            self.text.append(data)


CHUNK = 65536  # bytes of HTML per feed() call (64 KB); most leads are found in the first chunk
def first_paragraph(html: bytes) -> str: # "" if no paragraph was long enough (title-only page)
    parser = _FirstParagraph()  

    # "ignore" drops bytes that aren't valid UTF-8 rather than crashing.
    decoder = codecs.getincrementaldecoder("utf-8")("ignore")
    try:
        for start in range(0, len(html), CHUNK):
            parser.feed(decoder.decode(html[start:start + CHUNK]))  # feed() calls the handle_* methods
    except _Found:
        pass  
    return parser.lead  


def read_entry(zim: Archive, i: int, fast: bool = False):
    """Return (id, title, lead, path, target_id) for an HTML page, or None for anything else.

    Redirects (real ones, and small meta refresh pages) get lead "" and the
    path and id of the page they point to, so they are searchable by title only.

    fast=True stores the title and path without reading the article body, so no
    first paragraph (and therefore no vector) is produced. Meta-refresh
    forwarding pages are not merged in this mode.
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
    html = bytes(content[:MAX_HTML_BYTES])
    del content, item
    refresh_url = _refresh_url(html)
    
    if refresh_url:
        url = unquote(refresh_url).split("#", 1)[0]
        path = posixpath.normpath(posixpath.join(posixpath.dirname(entry.path), url))
        if not zim.has_entry_by_path(path):
            return None
        target = zim.get_entry_by_path(path)
        return i, entry.title, "", target.path, target._index
    return i, entry.title, first_paragraph(html), entry.path, None

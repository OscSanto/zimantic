import gzip
import hashlib
import json
import logging
import os
import re
import signal
import socket
import sys
import threading
import time
from contextlib import contextmanager
from html import escape as html_escape
from html import unescape as html_unescape
from pathlib import Path
from urllib.parse import quote as quote_path
from urllib.parse import urljoin, urlsplit

import uvicorn
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import Response, StreamingResponse

from ..content.serving import browser_mimetype, content_disposition, is_inline_mimetype
from ..content.store import (
    ArticleNotFoundError,
    ArticleTooLargeError,
    ArticleUnavailableError,
)
from ..diagnostics import public_cause
from ..paths import server_pid_path
from ..searching.sources import _html_attribute
from ..settings import normalize_web_base_path

ASSET_DIR = Path(__file__).parent / "static"
PAGE = ASSET_DIR / "index.html"
APP_CSS = ASSET_DIR / "app.css"
APP_JS = ASSET_DIR / "app.js"
I18N = ASSET_DIR / "i18n.js"
THEME = ASSET_DIR / "theme.js"
INDEXEDDB_SHIM = ASSET_DIR / "indexeddb-shim.js"
FAVICON = ASSET_DIR / "favicon.ico"
FAVICON_SVG = ASSET_DIR / "favicon.svg"
FAVICON_16 = ASSET_DIR / "favicon-16x16.png"
FAVICON_32 = ASSET_DIR / "favicon-32x32.png"
APPLE_TOUCH_ICON = ASSET_DIR / "apple-touch-icon.png"
ANDROID_CHROME_192 = ASSET_DIR / "android-chrome-192x192.png"
ANDROID_CHROME_512 = ASSET_DIR / "android-chrome-512x512.png"
WEB_MANIFEST = ASSET_DIR / "site.webmanifest"
MATHJAX_FONT_DIR = ASSET_DIR / "mathjax" / "HTML-CSS" / "TeX" / "woff"
_MATHJAX_FONT_RE = re.compile(
    r"(?:^|/)fonts/HTML-CSS/TeX/woff/(MathJax_[A-Za-z0-9_.-]+\.woff)$"
)
logger = logging.getLogger(__name__)


def _read_static_text(name: str) -> str:
    return (ASSET_DIR / name).read_text(encoding="utf-8").strip()


_ARTICLE_ERROR_STATUS: dict[type[Exception], int] = {
    ArticleNotFoundError: 404,
    ArticleTooLargeError: 413,
    ArticleUnavailableError: 503,
}

# Result payloads are text-heavy; gzip cuts them several-fold over the network.
GZIP_MIN_SIZE = 512
ARTICLE_STREAM_THRESHOLD = 512 * 1024
ARTICLE_STREAM_CHUNK = 64 * 1024
# Sandboxed article frames have an opaque origin, so in-article fetches need
# explicit CORS access to the public raw-entry route.  Actual responses also
# expose the range/validator metadata that media and conditional scripts read.
ARTICLE_CORS_HEADERS = {
    "Access-Control-Allow-Origin": "*",
    "Access-Control-Expose-Headers": (
        "Content-Range, Accept-Ranges, ETag, Last-Modified"
    ),
}
# Both HTML flavors receive the article bridge, mobile fallback, and head
# metadata handling.
HTML_MIMETYPES = frozenset({"text/html", "application/xhtml+xml"})

# Static assets are immutable for a given content version. The page links to
# app.css, app.js and i18n.js with a content-derived query string, so those can
# be cached for a year. Assets that the page or an article loads without a
# version (shims, icons, manifest) get a short cache instead.
STATIC_IMMUTABLE_SECONDS = 31536000
STATIC_SHORT_SECONDS = 3600
_COMPRESSIBLE_MIME_PREFIXES = (
    "text/",
    "application/javascript",
    "application/json",
    "application/xml",
    "image/svg",
)

# Precompress static files once per process. GZipMiddleware otherwise re-runs
# level-9 compression for every cold request; app.js is ~190 KB and costs
# around 20 ms on a Raspberry Pi, once per page load.
_STATIC_CACHE: dict[str, tuple[bytes, bytes | None, str]] = {}
_STATIC_CACHE_LOCK = threading.Lock()


def _compressible(media_type: str) -> bool:
    return media_type.startswith(_COMPRESSIBLE_MIME_PREFIXES)


def _cached_asset(
    key: str,
    loader,
    media_type: str,
) -> tuple[bytes, bytes | None, str]:
    cached = _STATIC_CACHE.get(key)
    if cached is not None:
        return cached
    data = loader()
    compressed = (
        gzip.compress(data, compresslevel=6)
        if _compressible(media_type) and len(data) >= GZIP_MIN_SIZE
        else None
    )
    etag = '"' + hashlib.sha1(data).hexdigest() + '"'
    cached = (data, compressed, etag)
    with _STATIC_CACHE_LOCK:
        cached = _STATIC_CACHE.setdefault(key, cached)
    return cached


def _cached_response(
    request: Request,
    key: str,
    loader,
    media_type: str,
    cache_control: str,
) -> Response:
    data, compressed, etag = _cached_asset(key, loader, media_type)
    headers = {
        "ETag": etag,
        "Cache-Control": cache_control,
        "Vary": "Accept-Encoding",
    }
    if request.headers.get("if-none-match") == etag:
        return Response(status_code=304, headers=headers)
    if compressed is not None and "gzip" in request.headers.get(
        "accept-encoding", ""
    ).lower():
        headers["Content-Encoding"] = "gzip"
        return Response(content=compressed, media_type=media_type, headers=headers)
    return Response(content=data, media_type=media_type, headers=headers)


def _static_version() -> str:
    """Version for the assets referenced by the page.

    Uses file metadata rather than file contents: this runs on each static
    request, and mtime plus size is enough to change the URL when an asset is
    deployed. The asset's own ETag still hashes its bytes.
    """
    digest = hashlib.sha1()
    for path in (APP_CSS, APP_JS, I18N, THEME):
        stat = path.stat()
        digest.update(f"{path}:{stat.st_mtime_ns}:{stat.st_size}".encode())
    return digest.hexdigest()[:12]


def _static_key(path: Path) -> str:
    """Cache key that changes when the file changes, even without a restart."""
    stat = path.stat()
    return f"{path}:{stat.st_mtime_ns}:{stat.st_size}"


def _versioned_page() -> bytes:
    document = PAGE.read_text(encoding="utf-8")
    version = _static_version()
    document = document.replace(
        'href="app.css"', f'href="app.css?v={version}"'
    )
    document = document.replace('src="app.js"', f'src="app.js?v={version}"')
    document = document.replace('src="i18n.js"', f'src="i18n.js?v={version}"')
    document = document.replace('src="theme.js"', f'src="theme.js?v={version}"')
    return document.encode("utf-8")


def page_response(request: Request, debug: bool = False) -> Response:
    """Serve the app shell with versioned asset URLs."""
    cache_control = "no-store" if debug else "no-cache"
    return _cached_response(request, "page", _versioned_page, "text/html", cache_control)


def static_response(
    request: Request,
    path: Path,
    media_type: str,
    *,
    versioned: bool = False,
    debug: bool = False,
) -> Response:
    """Serve one static asset with cached compression and a cache policy.

    Versioned assets are addressed with the current content version in the
    query string, so only that exact URL is immutable. A stale or unversioned
    URL falls back to revalidation instead of pinning old bytes in the browser.
    """
    if debug:
        cache_control = "no-store"
    elif versioned and request.query_params.get("v") == _static_version():
        cache_control = f"public, max-age={STATIC_IMMUTABLE_SECONDS}, immutable"
    elif versioned:
        cache_control = "no-cache"
    else:
        cache_control = f"public, max-age={STATIC_SHORT_SECONDS}"
    return _cached_response(
        request,
        _static_key(path),
        path.read_bytes,
        media_type,
        cache_control,
    )

# Some JavaScript applications read ``document.cookie`` even when they do not
# use cookies. The browser throws for that getter in the opaque-origin article
# sandbox used by secure browsing; expose the honest empty value so those
# applications can start.
ARTICLE_COOKIE_SHIM = (
    '<script data-zimantic-cookie-shim="true">\n'
    + _read_static_text("article-cookie-shim.js")
    + "\n</script>"
)


# Web Storage is unavailable in the opaque-origin article sandbox used by
# secure browsing. Read access throws a SecurityError, which aborts the
# application that owns the page (freeCodeCamp's Pinia store is one example).
# Keep the page running with an in-memory store and tell the shell. When secure
# browsing is off, the article stays same-origin and uses real browser storage.
ARTICLE_STORAGE_SHIM = (
    '<script data-zimantic-storage-shim="true">\n'
    + _read_static_text("article-storage-shim.js")
    + "\n</script>"
)


ARTICLE_INDEXEDDB_LOADER = """<script data-zimantic-indexeddb-loader="true">
(() => {
  // Secure browsing's sandbox has an opaque origin, where IndexedDB is
  // unavailable. Load the in-memory polyfill synchronously so page scripts see it.
  if (self.origin !== "null") return;
  const url = %s;
  document.write(
    '<script src="' + url + '" data-zimantic-indexeddb-shim="true"><' + '/script>'
  );
})();
</script>"""


CONTENT_SHELL_STYLE = (
    '<style data-zimantic-shell>\n'
    + _read_static_text("content-shell.css")
    + "\n</style>"
)

# The article shell is its own document; apply the saved theme before its
# toolbar styles paint so an explicit light/dark choice is not lost there.
CONTENT_SHELL_THEME = (
    '<script data-zimantic-theme>\n'
    + _read_static_text("theme.js")
    + "\n</script>"
)


ARTICLE_TOOLBAR_STYLE = (
    '<style data-zimantic-toolbar>\n'
    + _read_static_text("article-toolbar.css")
    + "\n</style>"
)

ARTICLE_TOOLBAR_SCRIPT = (
    '<script data-zimantic-toolbar>\n'
    + _read_static_text("i18n.js")
    + "\n"
    + _read_static_text("article-toolbar.js")
    + "\n</script>"
)


# Injected into raw HTML articles that predate mobile browsers. A page is
# treated as mobile-aware the moment it declares a viewport meta, and then it is
# left completely alone: its own stylesheet knows best. Only captures that never
# opted in get the reader fallback below.
ARTICLE_VIEWPORT_META = (
    '<meta name="viewport" content="width=device-width, initial-scale=1" />'
)

ARTICLE_MOBILE_STYLE = (
    '<style data-zimantic-mobile="true">\n'
    + _read_static_text("article-mobile.css")
    + "\n</style>"
)

# A few static captures ship a responsive layout that their own JavaScript only
# enables after it detects a phone (DevDocs' `_mobile` class is the clearest
# case). A desktop-only capture never names the class, so the desktop layout
# stays collapsed on a phone. Each hint maps a markup marker to the class that
# switches the page's own mobile styles on. The class is toggled through a
# media query, so wide screens keep the layout the capture shipped with.
MOBILE_LAYOUT_HINTS = (
    ("<devdocs-navbar", "_mobile"),
)

MOBILE_LAYOUT_SCRIPT = """<script data-zimantic-mobile="true">
(() => {
  const classes = %s;
  const narrow = window.matchMedia("(max-width: 800px)");
  const apply = () => {
    for (const name of classes) {
      document.documentElement.classList.toggle(name, narrow.matches);
    }
  };
  apply();
  if (narrow.addEventListener) narrow.addEventListener("change", apply);
  else if (narrow.addListener) narrow.addListener(apply);
})();
</script>"""


# Injected into raw HTML articles served under /article/. The article itself
# has no toolbar and makes no navigation decisions: this thin bridge filters
# clicks that should stay in the browser, then reports the authored href and
# any <base> to the /content/ shell, which owns navigation, offline-link
# mapping and the external-link notice. It reports the authored href (rather
# than the rewritten href property) so replay shims cannot turn relative links
# into live-site URLs. A top-level /article/ visit hands off to the shell so
# both routes offer one browsing experience.
ARTICLE_NAV_BRIDGE_SCRIPT = (
    '<script data-zimantic-bridge="true">\n'
    + _read_static_text("article-nav-bridge.js")
    + "\n</script>"
)


def _route_url(
    route: str, source_key: str, path: str | None = None, base_path: str = ""
) -> str:
    url = f"{base_path}/{route}/{quote_path(source_key, safe='')}"
    if path:
        url += f"/{quote_path(path, safe='/')}"
    return url


def _content_url(
    source_key: str, path: str | None = None, base_path: str = ""
) -> str:
    return _route_url("content", source_key, path, base_path)


def _article_url(
    source_key: str, path: str | None = None, base_path: str = ""
) -> str:
    return _route_url("article", source_key, path, base_path)


def _append_highlight(url: str, highlight: str | None = None) -> str:
    """Add the shared-text selection to a shell or raw article URL."""
    if not highlight:
        return url
    separator = "&" if "?" in url else "?"
    return f"{url}{separator}highlight={quote_path(highlight, safe='')}"


def _article_http_error(error: Exception) -> HTTPException:
    # ArticleTooLargeError subclasses ArticleUnavailableError, so test the
    # specific exceptions first (the mapping preserves that order).
    status = next(
        (
            status_code
            for error_type, status_code in _ARTICLE_ERROR_STATUS.items()
            if isinstance(error, error_type)
        ),
        500,
    )
    # Missing entries are fetched from opaque-origin article sandboxes too.
    # Return the same CORS grant as successful assets so a missing file reads
    # as an honest 404 instead of a misleading CORS failure.
    return HTTPException(
        status_code=status,
        detail=str(error),
        headers=ARTICLE_CORS_HEADERS,
    )


def _search_base_path(search) -> str:
    """Normalized reverse-proxy path prefix for the given Search instance."""
    cfg = getattr(search, "cfg", {}) or {}
    return normalize_web_base_path(cfg.get("web_base_path"))


_ZIM_DATE_SUFFIX_RE = re.compile(r"(?:[_-]20\d{2}-\d{2}(?:-\d{2})?)$")
_ZIM_LANGUAGE_SUFFIX_RE = re.compile(r"_(?:[a-z]{2,3}(?:-[a-z0-9]+)?)(?:_|$)")


def _zim_site_name(zim: str) -> str:
    """Short site name for the toolbar's home link.

    Kiwix stems look like ``<site>_<language>_<flavour>_<date>``. The leading
    component is usually the captured site's domain (``based.cooking``) or its
    project name (``dandwiki``), which reads much better in a toolbar than the
    full stem. The full stem stays available in the link's tooltip.
    """
    stem = str(zim).strip()
    without_date = _ZIM_DATE_SUFFIX_RE.sub("", stem)
    match = _ZIM_LANGUAGE_SUFFIX_RE.search(without_date)
    if match:
        site = without_date[: match.start()]
        if site:
            return site
    return without_date or stem


def _same_site_segment(segment: str, site_name: str) -> bool:
    left = segment.strip("/").casefold()
    right = site_name.strip("/").casefold()
    if not left or not right:
        return False
    return left == right or left.removeprefix("www.") == right.removeprefix("www.")


def _article_toolbar(
    zim: str,
    article: dict,
    base_path: str = "",
    highlight: str | None = None,
    path_exists=None,
    error: dict | None = None,
    show_zim_search: bool = True,
    source_previous: dict | None = None,
    source_next: dict | None = None,
    source_title: str | None = None,
    source_provider: str | None = None,
) -> str:
    zim_url = _content_url(zim, base_path=base_path)
    random_url = f"{base_path}/random?source={quote_path(zim, safe='')}"
    zim_name = html_escape(str(zim), quote=True)
    site_name = str(source_title or "").strip() or _zim_site_name(zim)
    provider = str(source_provider or "").strip()
    is_zim = not provider or provider in {"zim", "kiwix"}
    search_label = "Search this ZIM" if is_zim else "Search this source"
    article_url_path = str(article.get("path") or "").strip()
    trailing_slash = article_url_path.endswith("/")
    article_path = article_url_path.strip("/")
    path_parts = [part for part in article_path.split("/") if part]
    permalink_url = _append_highlight(
        _content_url(
            zim,
            article_path + "/" if trailing_slash and article_path else article_path,
            base_path,
        ),
        highlight,
    )
    raw_url = _append_highlight(
        _article_url(zim, article_url_path, base_path), highlight
    )
    article_title = html_escape(
        str(article.get("title") or article_path or "Permalink"),
        quote=False,
    )

    def parent_exists(parent_path: str) -> bool:
        if not callable(path_exists):
            return True
        try:
            return bool(path_exists(parent_path))
        except Exception:
            return False

    if error:
        # The requested entry is not readable (or the source is missing), so a
        # breadcrumb trail and a permalink would point at broken pages. Keep
        # Close and the site/home controls; the error panel carries the detail.
        breadcrumb_html = ""
        random_html = ""
    else:
        # ZIMs commonly use the first path segment as a container rather than an
        # article: zimit captures start with the site's domain, MediaWiki archives
        # with a one-letter first-letter bucket, and some tools add other folders.
        # The toolbar home link already names the site, so don't render that first
        # segment as a (usually broken) breadcrumb link.
        start = 0
        if len(path_parts) > 1:
            first = path_parts[0]
            if (
                _same_site_segment(first, site_name)
                or (len(first) == 1 and first.isalpha())
                or not parent_exists(first)
            ):
                start = 1

        breadcrumbs = []
        for depth in range(start + 1, len(path_parts)):
            parent_path = "/".join(path_parts[:depth])
            if trailing_slash:
                parent_path += "/"
            label = html_escape(path_parts[depth - 1])
            if parent_exists(parent_path):
                parent_url = _content_url(zim, parent_path, base_path)
                breadcrumbs.append(
                    f'<span class="zimantic-toolbar-separator" aria-hidden="true">›</span>'
                    f'<a class="zimantic-toolbar-breadcrumb" href="{parent_url}" '
                    f'title="Open {html_escape(parent_path, quote=True)}">'
                    f'{label}</a>'
                )
            else:
                breadcrumbs.append(
                    f'<span class="zimantic-toolbar-separator" aria-hidden="true">›</span>'
                    f'<span class="zimantic-toolbar-breadcrumb">{label}</span>'
                )
        breadcrumb_html = "".join(breadcrumbs) + (
            '<span class="zimantic-toolbar-separator" aria-hidden="true">›</span>'
            '<span class="zimantic-toolbar-article">'
            f'<a class="zimantic-toolbar-article-full" data-zimantic-raw-article '
            f'target="_blank" rel="noopener" '
            f'href="{raw_url}">{article_title}</a>'
            f'<a class="zimantic-toolbar-article-short" data-zimantic-permalink '
            f'data-zimantic-action="copy" href="{permalink_url}" '
            f'title="Open or copy this article\'s permalink" '
            f'data-i18n-title="Open or copy this article\'s permalink">'
            '<span data-i18n="Permalink">Permalink</span></a></span>'
        )
        random_html = (
            f'<a href="{random_url}" data-zimantic-action="random" '
            f'data-zimantic-random-zim-url="{html_escape(base_path + "/random", quote=True)}" '
            f'title="Open a random article (Alt-click for a random ZIM)" '
            f'data-i18n-title="Open a random article (Alt-click for a random ZIM)" '
            f'data-i18n="Random">Random</a>'
        )

    home_html = ""
    zim_search_html = ""
    if error is None or error.get("source_found"):
        home_html = (
            f'<a class="zimantic-toolbar-home" data-zimantic-home href="{zim_url}" '
            f'title="Open the homepage of {html_escape(site_name, quote=True)}">'
            f'<span class="zimantic-toolbar-home-full">{html_escape(site_name)}</span>'
            '<span class="zimantic-toolbar-home-short" data-i18n="Home">Home</span>'
            "</a>"
        )
        if show_zim_search:
            zim_search_html = (
                '<form class="zimantic-toolbar-zim-search" role="search" '
                'data-zimantic-zim-search '
                f'data-zimantic-zim="{zim_name}">'
                '<input type="search" class="zimantic-toolbar-zim-search-input" '
                f'data-zimantic-zim-search-input placeholder="{search_label}" '
                f'data-i18n-placeholder="{search_label}" '
                f'aria-label="{search_label}" data-i18n-aria-label="{search_label}" '
                'autocomplete="off" '
                'role="combobox" aria-autocomplete="list" aria-expanded="false" '
                'aria-controls="zimanticZimSuggestions">'
                '<div class="zimantic-toolbar-suggestions" '
                'id="zimanticZimSuggestions" role="listbox" '
                'data-zimantic-zim-suggestions hidden></div>'
                "</form>"
            )
    library_search_html = "" if zim_search_html else (
        f'<a class="zimantic-toolbar-search" href="{base_path}/" data-zimantic-action="home" '
        'title="Search your library" data-i18n-title="Search your library">Zimantic</a>'
    )
    source_nav_html = ""
    if source_previous is not None and source_next is not None:
        def source_button(
            action: str,
            attribute: str,
            label: str,
            title_prefix: str,
            target: dict,
            arrow: str,
        ) -> str:
            title = html_escape(
                f"{title_prefix}: {target.get('title') or target.get('key') or ''}",
                quote=True,
            )
            url = html_escape(str(target.get("url") or ""), quote=True)
            return (
                f'<button type="button" data-zimantic-action="{action}" '
                f'{attribute} data-zimantic-url="{url}" '
                f'aria-label="{label}" data-i18n-aria-label="{label}" '
                f'title="{title}">{arrow}</button>'
            )

        source_nav_html = (
            '<span class="zimantic-toolbar-source-nav" role="group" '
            'aria-label="Selected ZIMs" data-i18n-aria-label="Selected ZIMs">'
            + source_button(
                "source-previous",
                "data-zimantic-source-previous",
                "Previous ZIM",
                "Previous ZIM",
                source_previous,
                "←",
            )
            + source_button(
                "source-next",
                "data-zimantic-source-next",
                "Next ZIM",
                "Next ZIM",
                source_next,
                "→",
            )
            + "</span>"
        )
    return f"""<nav data-zimantic-toolbar aria-label="Article navigation">
  <div class="zimantic-toolbar-nav">
    <button type="button" data-zimantic-action="close" title="Close article"
      data-i18n="Close" data-i18n-title="Close article">Close</button>
    <span class="zimantic-toolbar-context-nav" role="group"
      aria-label="Adjacent search results" data-i18n-aria-label="Adjacent search results">
      <button type="button" data-zimantic-action="previous"
        data-zimantic-context-previous hidden disabled aria-label="Previous search result"
        data-i18n-aria-label="Previous search result"
        title="Open the previous search result"
        data-i18n-title="Open the previous search result">←</button>
      <button type="button" data-zimantic-action="next"
        data-zimantic-context-next hidden disabled aria-label="Next search result"
        data-i18n-aria-label="Next search result"
        title="Open the next search result"
        data-i18n-title="Open the next search result">→</button>
    </span>
    {source_nav_html}
    {random_html}
    {home_html}
    <span class="zimantic-toolbar-breadcrumbs">{breadcrumb_html}</span>
  </div>
  <div class="zimantic-toolbar-actions">
    <span class="zimantic-toolbar-parent-nav-group" role="group"
      aria-label="Adjacent links in parent page"
      data-i18n-aria-label="Adjacent links in parent page">
      <button type="button" class="zimantic-toolbar-parent-nav"
        data-zimantic-action="parent-previous" data-zimantic-parent-previous
        hidden disabled aria-label="Previous link in parent page"
        data-i18n-aria-label="Previous link in parent page"
        title="Open the previous link in the parent page"
        data-i18n-title="Open the previous link in the parent page">↑</button>
      <button type="button" class="zimantic-toolbar-parent-nav"
        data-zimantic-action="parent-next" data-zimantic-parent-next
        hidden disabled aria-label="Next link in parent page"
        data-i18n-aria-label="Next link in parent page"
        title="Open the next link in the parent page"
        data-i18n-title="Open the next link in the parent page">↓</button>
    </span>
    {library_search_html}
    {zim_search_html}
  </div>
  <span class="zimantic-toolbar-status" data-zimantic-toolbar-status aria-live="polite"></span>
</nav>"""


_SCRIPT_BLOCK = re.compile(
    r"(<script\b[^>]*>)(.*?)(</script\s*>)", re.IGNORECASE | re.DOTALL
)
_STYLE_BLOCK = re.compile(
    r"(<style\b[^>]*>)(.*?)(</style\s*>)", re.IGNORECASE | re.DOTALL
)


def _xml_safe_scripts(markup: str) -> str:
    """Wrap injected inline scripts in CDATA for XHTML documents."""
    def replace(match: re.Match[str]) -> str:
        opening, body, closing = match.groups()
        if "//<![CDATA[" in body:
            return match.group(0)
        return f"{opening}\n//<![CDATA[\n{body}\n//]]>\n{closing}"

    return _SCRIPT_BLOCK.sub(replace, markup)


def _xml_safe_styles(markup: str) -> str:
    """Wrap injected stylesheets in CSS-compatible CDATA for XHTML documents."""
    def replace(match: re.Match[str]) -> str:
        opening, body, closing = match.groups()
        if "/*<![CDATA[*/" in body:
            return match.group(0)
        return f"{opening}\n/*<![CDATA[*/\n{body}\n/*]]>*/\n{closing}"

    return _STYLE_BLOCK.sub(replace, markup)


def _json_for_script(value) -> str:
    """Serialize a value for an inline script without allowing ``</script>``."""
    return json.dumps(value).replace("<", "\\u003c")


def _decorate_article_bridge(
    content: bytes | memoryview, zim: str, base_path: str = "", xhtml: bool = False
) -> bytes | memoryview:
    """Add the link-reporting bridge and mobile fallback to a raw article.

    The article keeps its own markup; a small script filters and reports link
    clicks to the /content/ shell, and desktop-only captures also get the mobile
    fallback described above. Non-HTML entries and documents that already carry
    the bridge are returned untouched.
    """
    try:
        document = bytes(content).decode("utf-8")
    except UnicodeDecodeError:
        return content
    if "data-zimantic-bridge" in document:
        return content

    document, mobile_head = _mobile_article_head(document)
    # Install the storage shims before the page's own scripts: an inline script
    # that reads localStorage or opens IndexedDB would otherwise throw before
    # the shim exists.
    early = ARTICLE_INDEXEDDB_LOADER % _json_for_script(
        f"{base_path}/indexeddb-shim.js"
    ) + ARTICLE_STORAGE_SHIM
    additions = (
        ARTICLE_COOKIE_SHIM
        + mobile_head
        + ARTICLE_NAV_BRIDGE_SCRIPT.replace(
            "__BASE_PATH_JSON__", _json_for_script(base_path)
        )
    )
    if xhtml:
        early = _xml_safe_scripts(early)
        additions = _xml_safe_styles(_xml_safe_scripts(additions))
    head_open = re.search(r"<head\b[^>]*>", document, re.IGNORECASE)
    if head_open:
        position = head_open.end()
        document = document[:position] + early + document[position:]
    else:
        html_open = re.search(r"<html\b[^>]*>", document, re.IGNORECASE)
        if html_open:
            position = html_open.end()
            document = document[:position] + early + document[position:]
        else:
            document = early + document
    head_close = re.search(r"</head\s*>", document, re.IGNORECASE)
    if head_close:
        position = head_close.start()
        document = document[:position] + additions + document[position:]
    else:
        body_open = re.search(r"<body\b[^>]*>", document, re.IGNORECASE)
        if body_open:
            position = body_open.start()
            document = document[:position] + additions + document[position:]
        else:
            html_open = re.search(r"<html\b[^>]*>", document, re.IGNORECASE)
            if html_open:
                position = html_open.end()
                document = document[:position] + additions + document[position:]
            else:
                document = additions + document
    return document.encode("utf-8")


def _mobile_article_head(document: str) -> tuple[str, str]:
    """Viewport, stylesheet and layout hints for a desktop-only capture.

    Returns the document plus the markup to inject into its ``<head>``.
    Documents that already declare a viewport meta are returned unchanged with
    no extra markup, because they are already mobile-aware.
    """
    if re.search(
        r"<meta\b[^>]*\bname\s*=\s*[\"']?viewport", document, re.IGNORECASE
    ):
        return document, ""
    head = ARTICLE_VIEWPORT_META + ARTICLE_MOBILE_STYLE
    classes = [
        class_name for marker, class_name in MOBILE_LAYOUT_HINTS
        if marker in document
    ]
    if classes:
        head += MOBILE_LAYOUT_SCRIPT % json.dumps(classes)
    return document, head


def _article_head_meta(
    content: bytes | memoryview, zim: str, path: str | None, base_path: str = ""
) -> tuple[str | None, str | None]:
    """Title and favicon declared by a raw article's ``<head>``.

    Relative icon hrefs are resolved against the article's own /article/ URL so
    the browser keeps loading them through the raw route. External, data: and
    non-/article/ icons are ignored, letting the caller fall back to the ZIM's
    source icon instead.
    """
    try:
        document = bytes(content).decode("utf-8", errors="replace")
    except (TypeError, ValueError):
        return None, None
    head_close = re.search(r"</head\s*>", document, re.IGNORECASE)
    head = document[: head_close.start()] if head_close else document[:65536]

    title = None
    title_match = re.search(
        r"<title[^>]*>(.*?)</title>", head, re.IGNORECASE | re.DOTALL
    )
    if title_match:
        title = html_unescape(re.sub(r"\s+", " ", title_match.group(1))).strip() or None

    base = _article_url(zim, path, base_path) + ("" if path else "/")
    icon_prefix = f"{base_path}/article/"
    icon = None
    for tag in re.findall(r"<link\b[^>]*>", head, re.IGNORECASE):
        if "icon" not in _html_attribute(tag, "rel").casefold():
            continue
        href = _html_attribute(tag, "href").strip()
        if not href:
            continue
        parsed = urlsplit(urljoin(base, href))
        if parsed.scheme or parsed.netloc or not parsed.path.startswith(icon_prefix):
            continue
        icon = parsed.path + (f"?{parsed.query}" if parsed.query else "")
        break
    return title, icon


def _content_error_panel(error: dict, base_path: str = "") -> str:
    """Friendly error body used when an article or source cannot be read."""
    title = html_escape(str(error.get("title") or "Article unavailable"))
    message = html_escape(str(error.get("message") or ""))
    path = html_escape(str(error.get("path") or ""))
    path_html = (
        f'<p class="zimantic-shell-error-path">Requested path: <code>{path}</code></p>'
        if path
        else ""
    )
    return (
        '<div class="zimantic-shell-error" role="alert">'
        f"<h1>{title}</h1>"
        f"<p>{message}</p>"
        f"{path_html}"
        f'<p class="zimantic-shell-error-links">'
        f'<a href="{base_path}/" data-zimantic-search-home>Search your library</a>'
        "</p>"
        "</div>"
    )


def _content_attachment_panel(
    title: str, download_url: str, mimetype: str | None
) -> str:
    """Explain a non-inline attachment instead of loading a blank iframe."""

    kind = html_escape(str(mimetype or "file"), quote=True)
    title_html = html_escape(str(title), quote=False)
    return (
        '<div class="zimantic-shell-attachment" role="region">'
        f'<h1 class="zimantic-shell-attachment-title">{title_html}</h1>'
        '<p data-i18n="This file cannot be displayed in the offline viewer.">'
        "This file cannot be displayed in the offline viewer.</p>"
        f'<p class="zimantic-shell-attachment-type"><code>{kind}</code></p>'
        f'<p><a class="zimantic-shell-download" href="{html_escape(download_url, quote=True)}" '
        'download data-i18n="Download file">Download file</a></p>'
        "</div>"
    )


def _content_shell(
    zim: str,
    article: dict,
    status_code: int = 200,
    fallback_icon: str | None = None,
    base_path: str = "",
    highlight: str | None = None,
    path_exists=None,
    error: dict | None = None,
    secure_browsing: bool = False,
    show_zim_search: bool = True,
    source_previous: dict | None = None,
    source_next: dict | None = None,
    source_title: str | None = None,
    source_provider: str | None = None,
) -> Response:
    """Toolbar shell page whose iframe loads the raw article from /article/.

    Keeping the toolbar in this outer document means it survives any
    navigation inside the iframe, including 404s and other error bodies. The
    shell also mirrors the article's title and favicon: directly loaded shells
    use them for the browser tab, and embedded shells forward them to the host
    so the modal's tab follows the article too. Unreadable entries get a
    friendly error panel instead of rendering the raw JSON error body.
    """
    article_url_path = str(article.get("path") or "").strip()
    article_path = article_url_path.strip("/")
    iframe_url = _append_highlight(
        _article_url(zim, article_url_path, base_path),
        highlight,
    )
    iframe_sandbox = (
        ' sandbox="allow-scripts allow-popups allow-popups-to-escape-sandbox"'
        if secure_browsing
        else ""
    )
    attachment = (
        not error
        and bool(article.get("mimetype"))
        and not is_inline_mimetype(article.get("mimetype"))
    )
    toolbar = _article_toolbar(
        zim,
        article,
        base_path,
        highlight,
        path_exists,
        error=error,
        show_zim_search=show_zim_search,
        source_previous=source_previous,
        source_next=source_next,
        source_title=source_title,
        source_provider=source_provider,
    )
    entry_title = str((error or {}).get("title") or article.get("title") or article_path or zim)
    parsed_title = None
    icon = fallback_icon
    if not error and article.get("mimetype") in HTML_MIMETYPES and article.get("content") is not None:
        parsed_title, parsed_icon = _article_head_meta(
            article["content"], zim, article_url_path or None, base_path
        )
        icon = parsed_icon or fallback_icon
    # quote=True so an article title containing a quote cannot break out of the
    # iframe's title="..." attribute; entities remain valid in <title> too.
    title = html_escape(parsed_title or entry_title, quote=True)
    icon_link = (
        f'<link rel="icon" data-zimantic-article-icon href="{html_escape(icon, quote=True)}">\n'
        if icon
        else ""
    )
    html = (
        "<!doctype html>\n"
        '<html lang="en">\n'
        "<head>\n"
        '<meta charset="utf-8">\n'
        '<meta name="viewport" content="width=device-width, initial-scale=1">\n'
        f"<title>{title}</title>\n"
        + icon_link
        + CONTENT_SHELL_THEME
        + ARTICLE_TOOLBAR_STYLE
        + CONTENT_SHELL_STYLE
        + "\n</head>\n<body>\n"
        + toolbar
        + '<main data-zimantic-shell-main>'
        + (
            _content_error_panel(error, base_path)
            if error
            else _content_attachment_panel(
                parsed_title or entry_title, iframe_url, article.get("mimetype")
            )
            if attachment
            else (
                f'<h1 class="zimantic-shell-heading">{title}</h1>\n'
                "\n<div id=\"articleStorageNotice\" class=\"article-storage-notice\" "
                "data-zimantic-storage-notice role=\"status\" hidden>\n"
                '<p data-i18n="This article uses browser storage, which is isolated '
                'inside this viewer. Changes may not be saved.">This article uses '
                "browser storage, which is isolated inside this viewer. Changes may "
                "not be saved.</p>\n"
                "<a id=\"articleStorageNoticeOpen\" class=\"secondary\" "
                "data-zimantic-storage-notice-open href=\"#\" target=\"_blank\" "
                "rel=\"noopener noreferrer\"><span data-i18n=\"Open in a new tab\">"
                "Open in a new tab</span> "
                "<span aria-hidden=\"true\">↗</span></a>\n"
                "<button id=\"articleStorageNoticeDismiss\" class=\"secondary\" "
                "data-zimantic-storage-notice-dismiss type=\"button\" "
                "data-i18n=\"Dismiss\">Dismiss</button>\n"
                "</div>\n"
                + "\n<div data-zimantic-capture-notice hidden>"
                '<p data-i18n="This capture has no readable content. The archived '
                'page may have been empty (for example, a parked domain), or its '
                'scripts and styles may not have been saved.">This capture has no '
                "readable content. The archived page may have been empty (for "
                "example, a parked domain), or its scripts and styles may not have "
                "been saved.</p>"
                '<button type="button" data-zimantic-capture-notice-dismiss '
                'data-i18n="Dismiss">Dismiss</button>'
                "</div>\n"
                + f'<iframe data-zimantic-article title="{title}" '
                f'src="{iframe_url}" referrerpolicy="same-origin"{iframe_sandbox}></iframe>\n'
            )
        )
        + "</main>"
        + ARTICLE_TOOLBAR_SCRIPT.replace(
            "__APP_BASE_PATH_JSON__", _json_for_script(base_path)
        ).replace(
            "__SECURE_BROWSING_JSON__", json.dumps(bool(secure_browsing))
        )
        + "\n</body>\n</html>"
    )
    return Response(
        content=html.encode("utf-8"),
        media_type="text/html",
        status_code=status_code,
        headers={"Cache-Control": "no-cache"},
    )


def _external_content_shell(url: str, title: str | None = None) -> Response:
    """Hand an external content URL to the host page for a top-level redirect.

    A running Kolibri server sends ``X-Frame-Options: DENY`` and its own CSP,
    so it cannot be framed inside the article modal.  The shell is same-origin
    with the host, though, so it can ask the host to navigate the whole page
    instead of trapping the external document in the iframe.  A direct visit
    falls back to a normal replace, and the link works without scripts.
    """
    target = str(url)
    label = html_escape(str(title or "content"), quote=True)
    target_attr = html_escape(target, quote=True)
    script_target = json.dumps(target).replace("<", "\\u003c")
    html = (
        "<!doctype html>\n"
        '<html lang="en">\n'
        "<head>\n"
        '<meta charset="utf-8">\n'
        '<meta name="viewport" content="width=device-width, initial-scale=1">\n'
        f"<title>{label}</title>\n"
        "</head>\n<body>\n"
        f'<p>Opening <a href="{target_attr}" target="_top" '
        f'rel="noopener noreferrer">{label}</a>…</p>\n'
        "<script>\n"
        "(() => {\n"
        f"  const target = {script_target};\n"
        "  if (window.top === window) {\n"
        "    window.location.replace(target);\n"
        "    return;\n"
        "  }\n"
        "  try {\n"
        '    window.parent.postMessage(\n'
        '      { type: "zimantic-external-redirect", url: target },\n'
        "      window.location.origin,\n"
        "    );\n"
        "  } catch (_) {\n"
        "    window.location.replace(target);\n"
        "  }\n"
        "})();\n"
        "</script>\n"
        "</body>\n</html>"
    )
    return Response(
        content=html.encode("utf-8"),
        media_type="text/html",
        headers={"Cache-Control": "no-cache"},
    )


def _article_response_headers(
    mimetype: str,
    *,
    headers: dict[str, str] | None = None,
    filename: str | None = None,
) -> dict[str, str]:
    response_headers = {
        "Cache-Control": "no-cache",
        "X-Content-Type-Options": "nosniff",
        **ARTICLE_CORS_HEADERS,
        **(headers or {}),
    }
    if not is_inline_mimetype(mimetype):
        # Unknown binaries are attachments and must never be interpreted as
        # HTML by a browser that guesses from their first bytes.
        response_headers["Content-Disposition"] = content_disposition(
            mimetype, filename
        )
        response_headers["X-Content-Type-Options"] = "nosniff"
    elif str(mimetype).split(";", 1)[0].strip().lower() == "image/svg+xml":
        # Direct SVG views are active documents.  Sandbox them even though an
        # <img> reference to the same bytes is inert.
        response_headers["Content-Security-Policy"] = (
            "sandbox; default-src 'none'; style-src 'unsafe-inline'; "
            "img-src data:; font-src data:"
        )
        response_headers["X-Content-Type-Options"] = "nosniff"
    return response_headers


def _article_content_response(
    content: bytes | memoryview,
    mimetype: str,
    *,
    status_code: int = 200,
    headers: dict[str, str] | None = None,
    filename: str | None = None,
) -> Response:
    mimetype = browser_mimetype(mimetype)
    response_headers = _article_response_headers(
        mimetype, headers=headers, filename=filename
    )
    if len(content) > ARTICLE_STREAM_THRESHOLD:
        # Python-libzim exposes the decompressed item as one memoryview.
        # Chunking here avoids another whole-entry response copy and lets
        # the client consume large assets incrementally.
        def chunks():
            for start in range(0, len(content), ARTICLE_STREAM_CHUNK):
                yield bytes(content[start:start + ARTICLE_STREAM_CHUNK])

        return StreamingResponse(
            chunks(),
            media_type=mimetype,
            status_code=status_code,
            headers={
                **response_headers,
                "Content-Length": str(len(content)),
            },
        )
    return Response(
        content=content,
        media_type=mimetype,
        status_code=status_code,
        headers=response_headers,
    )


def _vendored_mathjax_font(path: str) -> Path | None:
    """Return a bundled MathJax WOFF font for a missing article font path."""
    match = _MATHJAX_FONT_RE.search(str(path or ""))
    if not match:
        return None
    name = match.group(1)
    if name != Path(name).name:
        return None
    root = MATHJAX_FONT_DIR.resolve()
    candidate = (root / name).resolve()
    try:
        candidate.relative_to(root)
    except ValueError:
        return None
    return candidate if candidate.is_file() else None


def _memoized_json(memo_key: str, version, payload_factory, memos):
    """Return a memoized ``(version, payload, body, ETag)`` tuple for one app instance."""
    cached = memos.get(memo_key)
    if cached is None or cached[0] != version:
        payload = payload_factory()
        body = json.dumps(payload, separators=(",", ":"), sort_keys=True, default=str)
        etag = '"' + hashlib.sha1(body.encode("utf-8")).hexdigest() + '"'
        memos[memo_key] = (version, payload, body, etag)
    return memos[memo_key]


def _json_response(request: Request, memo_key: str, version, payload_factory, memos) -> Response:
    """JSON response with a content ETag, memoized per app instance.

    Re-serializing and hashing a multi-megabyte payload on every poll would
    make ETag revalidation nearly as expensive as a full download, so the
    serialized body is reused until ``version`` changes. ``memos`` is owned by
    the calling app, so two app instances never share cached payloads.
    """
    _, _, body, etag = _memoized_json(memo_key, version, payload_factory, memos)
    headers = {"ETag": etag, "Cache-Control": "no-cache"}
    if request.headers.get("if-none-match") == etag:
        return Response(status_code=304, headers=headers)
    return Response(content=body, media_type="application/json", headers=headers)


def warm_static_cache() -> None:
    """Precompress the page shell and its assets before the first request.

    Without this, the first visitor after a restart pays the level-9 gzip cost
    for app.js (about 20 ms on a Raspberry Pi).
    """
    for path, media_type in (
        (APP_CSS, "text/css"),
        (APP_JS, "application/javascript"),
        (I18N, "application/javascript"),
        (THEME, "application/javascript"),
        (INDEXEDDB_SHIM, "application/javascript"),
    ):
        _cached_asset(_static_key(path), path.read_bytes, media_type)
    _cached_asset("page", _versioned_page, "text/html")


def create_app(searchClass, debug: bool = False) -> FastAPI:
    app = FastAPI(title="Zimantic")
    app.add_middleware(GZipMiddleware, minimum_size=GZIP_MIN_SIZE)
    from fastapi.routing import APIRoute

    from .routes import register_routes

    register_routes(app, searchClass, debug)
    # FastAPI does not add HEAD for a GET route. HEAD is part of correct HTTP
    # semantics and cheap for these JSON/shell endpoints; content routes add
    # their own efficient HEAD handlers.
    for route in app.routes:
        if isinstance(route, APIRoute) and route.methods == {"GET"}:
            route.methods.add("HEAD")
    return app


def _handle_reload_request(searchClass, guard: threading.Lock) -> None:
    """SIGHUP handler: reload in a worker thread so the event loop is not blocked
    while indexes are reopened. A second signal during a reload is ignored."""
    if not guard.acquire(blocking=False):
        logger.warning("zimantic: reload already in progress; ignoring signal")
        return

    def _reload():
        try:
            summary = searchClass.reload()
            logger.info(
                "zimantic: reloaded "
                f"{len(summary.get('indexes', []))} index(es) "
                f"(+{len(summary.get('added', []))} new, "
                f"~{len(summary.get('upgraded', []))} upgraded, "
                f"-{len(summary.get('removed', []))} removed)"
            )
        except Exception as error:  # never let a reload take the process down
            logger.error("zimantic: reload failed: %s", public_cause(error))
            logger.debug("reload failure: %s", error)
        finally:
            guard.release()

    threading.Thread(target=_reload, name="zimantic-reload", daemon=True).start()


def _handle_restart_request(_signum, _frame) -> None:
    """Replace the process so development changes and all server state reload."""
    logger.info("zimantic: restarting server after SIGHUP")
    os.execv(
        sys.executable,
        [sys.executable, "-m", "zimantic", *sys.argv[1:]],
    )


def _install_sighup_handlers(searchClass, debug: bool) -> None:
    """Install reload/restart handlers for a normal ``serve`` process."""
    sighup = getattr(signal, "SIGHUP", None)
    if sighup is None:
        return
    if debug:
        signal.signal(sighup, _handle_restart_request)
    else:
        guard = threading.Lock()
        signal.signal(
            sighup,
            lambda _signum, _frame: _handle_reload_request(searchClass, guard),
        )


def _close_search(searchClass) -> None:
    shutdown = getattr(searchClass, "close", None)
    if not callable(shutdown):
        return
    try:
        shutdown()
    except Exception as error:  # never mask the original shutdown path
        logger.warning(
            "zimantic: search shutdown failed (%s)", public_cause(error)
        )
        logger.debug("search shutdown error: %s", error)


def _write_pid_file(pid_file: Path) -> None:
    try:
        pid_file.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        pid_file.write_text(f"{os.getpid()}\n", encoding="utf-8")
    except OSError as error:
        logger.error(
            "zimantic: could not write the PID file (%s)",
            public_cause(error),
        )
        logger.debug("could not write PID file %s: %s", pid_file, error)


def bind_local_socket(host: str = "127.0.0.1", port: int = 0) -> socket.socket:
    """Pre-bind a listening socket so the OS-assigned port is known up front."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind((host, int(port)))
        sock.listen(2048)
        sock.setblocking(False)
    except BaseException:
        sock.close()
        raise
    return sock


_DEFAULT_PID = object()


class _GracefulSignalServer(uvicorn.Server):
    """Uvicorn server whose captured stop signal is not re-raised.

    ``zimantic open`` is a foreground command. A Ctrl-C or SIGTERM should run
    cleanup and return status 0; uvicorn's default restores the default handler
    and re-raises the signal, which turns a clean stop into shell status 143.
    ``serve`` keeps uvicorn's native behavior.
    """

    @contextmanager
    def capture_signals(self):
        if threading.current_thread() is not threading.main_thread():
            yield
            return
        original_handlers = {
            captured: signal.signal(captured, self.handle_exit)
            for captured in uvicorn.server.HANDLED_SIGNALS
        }
        try:
            yield
        finally:
            for captured, handler in original_handlers.items():
                signal.signal(captured, handler)


def run_server(
    searchClass,
    *,
    host: str = "0.0.0.0",
    port: int = 0,
    debug: bool = False,
    pid_path: Path | object | None = _DEFAULT_PID,
    prebound_socket: socket.socket | None = None,
    on_listening=None,
    install_sighup: bool = True,
    announce: bool = True,
) -> None:
    """Run the web app and close search resources on every exit path.

    ``serve`` keeps its historical defaults (all interfaces, configured port,
    PID file, SIGHUP reload/restart).  ``open`` passes a pre-bound loopback
    socket, no PID file, no SIGHUP handlers, and a startup callback.
    """
    app = create_app(searchClass, debug=debug)
    warm_static_cache()
    if pid_path is _DEFAULT_PID:
        pid_path = server_pid_path()
    if pid_path is not None:
        _write_pid_file(Path(pid_path))

    # Provider runtimes start their own bounded discovery in the background;
    # an unreachable Kiwix server cannot delay binding the port.
    if install_sighup:
        _install_sighup_handlers(searchClass, debug)

    server_mode = " (debug)" if debug else ""
    actual_port = int(port)
    if prebound_socket is not None:
        actual_port = int(prebound_socket.getsockname()[1])
    if announce:
        logger.info(
            f"zimantic: server{server_mode} loaded at "
            f"http://localhost:{actual_port}\n"
            f"zimantic: PID {os.getpid()}; reload with `zimantic reload` "
        )
    start_faiss = getattr(searchClass, "start_faiss", None)
    if callable(start_faiss):
        start_faiss()
    start_fulltext = getattr(searchClass, "start_fulltext_isolation", None)
    if callable(start_fulltext):
        start_fulltext()
    try:
        if prebound_socket is not None:
            config = uvicorn.Config(
                app,
                host=host,
                port=actual_port,
                log_level="warning",
            )
            server = _GracefulSignalServer(config)

            def _notify_listening() -> None:
                while not server.started and not server.should_exit:
                    time.sleep(0.02)
                if not server.started or on_listening is None:
                    return
                try:
                    on_listening(actual_port)
                except Exception as error:  # a browser failure must not stop serving
                    logger.warning(
                        "zimantic: startup callback failed (%s)",
                        public_cause(error),
                    )
                    logger.debug("startup callback error: %s", error)

            threading.Thread(
                target=_notify_listening,
                name="zimantic-listening",
                daemon=True,
            ).start()
            try:
                server.run(sockets=[prebound_socket])
            finally:
                try:
                    prebound_socket.close()
                except OSError:
                    pass
        else:
            uvicorn.run(app, host=host, port=actual_port, log_level="warning")
    finally:
        _close_search(searchClass)
        if pid_path is not None:
            try:
                Path(pid_path).unlink()
            except OSError:
                pass


def serve(searchClass, port: int, debug: bool = False) -> None:
    """Run a normal long-lived server over the configured library."""
    run_server(searchClass, host="0.0.0.0", port=port, debug=debug)

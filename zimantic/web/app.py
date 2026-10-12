import hashlib
from html import escape as html_escape
from html import unescape as html_unescape
import json
import logging
from pathlib import Path
import os
import re
import signal
import sys
import threading
from urllib.parse import quote as quote_path
from urllib.parse import urljoin, urlsplit
import uvicorn
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import Response, StreamingResponse
from ..search import (
    ArticleNotFoundError,
    ArticleTooLargeError,
    ArticleUnavailableError,
)
from ..searching.sources import _html_attribute

ASSET_DIR = Path(__file__).parent / "static"
PAGE = ASSET_DIR / "index.html"
APP_CSS = ASSET_DIR / "app.css"
APP_JS = ASSET_DIR / "app.js"
FAVICON = ASSET_DIR / "favicon.ico"
FAVICON_16 = ASSET_DIR / "favicon-16x16.png"
FAVICON_32 = ASSET_DIR / "favicon-32x32.png"
APPLE_TOUCH_ICON = ASSET_DIR / "apple-touch-icon.png"
ANDROID_CHROME_192 = ASSET_DIR / "android-chrome-192x192.png"
ANDROID_CHROME_512 = ASSET_DIR / "android-chrome-512x512.png"
WEB_MANIFEST = ASSET_DIR / "site.webmanifest"
PID_FILE = Path("zimantic.pid")
logger = logging.getLogger(__name__)

_ARTICLE_ERROR_STATUS = {
    ArticleNotFoundError: 404,
    ArticleTooLargeError: 413,
    ArticleUnavailableError: 503,
}

# Result payloads are text-heavy; gzip cuts them several-fold over the network.
GZIP_MIN_SIZE = 512
ARTICLE_STREAM_THRESHOLD = 512 * 1024
ARTICLE_STREAM_CHUNK = 64 * 1024
# Sandboxed article frames have an opaque origin, so in-article fetches need
# explicit CORS access to the public raw-entry route.
ARTICLE_CORS_HEADERS = {"Access-Control-Allow-Origin": "*"}


ARTICLE_TOOLBAR_STYLE = """<style data-zimantic-toolbar>
nav[data-zimantic-toolbar] {
  align-items: center !important;
  background: #171a21 !important;
  border-bottom: 1px solid #2a2f3a !important;
  box-shadow: 0 1px 5px rgba(0, 0, 0, .25) !important;
  color: #e6e8eb !important;
  display: flex !important;
  flex-wrap: wrap !important;
  font: 14px/1.3 system-ui, sans-serif !important;
  gap: 6px !important;
  margin: 0 0 16px !important;
  min-height: 0 !important;
  padding: 6px 10px !important;
  position: fixed !important;
  top: 0 !important;
  width: 100% !important;
  z-index: 2147483647 !important;
}
/* The <script>/<style> markers carry the same attribute; never let the layout
   rule above turn the injected script source into visible page text. */
script[data-zimantic-toolbar] { display: none !important; }
[data-zimantic-toolbar],
[data-zimantic-toolbar] * { box-sizing: border-box !important; }
[data-zimantic-toolbar] a,
[data-zimantic-toolbar] button {
  background: transparent !important;
  border: 1px solid #46505f !important;
  border-radius: 4px !important;
  color: #d7e5f7 !important;
  cursor: pointer !important;
  font: inherit !important;
  padding: 4px 8px !important;
  text-decoration: none !important;
}
[data-zimantic-toolbar] a:hover,
[data-zimantic-toolbar] button:hover {
  background: #263449 !important;
  border-color: #4f9cf9 !important;
}
[data-zimantic-toolbar] .zimantic-toolbar-nav,
[data-zimantic-toolbar] .zimantic-toolbar-actions {
  align-items: center !important;
  display: flex !important;
  flex-wrap: nowrap !important;
  gap: 6px !important;
  min-width: 0 !important;
}
[data-zimantic-toolbar] .zimantic-toolbar-actions {
  margin-left: auto !important;
}
[data-zimantic-toolbar] .zimantic-toolbar-context-nav {
  align-items: center !important;
  display: flex !important;
  flex: 0 0 auto !important;
  gap: 6px !important;
}
[data-zimantic-toolbar] .zimantic-toolbar-parent-nav-group {
  align-items: center !important;
  display: flex !important;
  flex: 0 0 auto !important;
  gap: 6px !important;
}
[data-zimantic-toolbar] .zimantic-toolbar-context-nav button:disabled {
  cursor: default !important;
  opacity: .45 !important;
}
[data-zimantic-toolbar] .zimantic-toolbar-parent-nav {
  flex: 0 0 auto !important;
  min-width: 30px !important;
  padding-left: 6px !important;
  padding-right: 6px !important;
}
[data-zimantic-toolbar] .zimantic-toolbar-home {
  max-width: min(32vw, 360px) !important;
  min-width: 0 !important;
  overflow: hidden !important;
  text-overflow: ellipsis !important;
  white-space: nowrap !important;
}
[data-zimantic-toolbar] .zimantic-toolbar-article {
  max-width: min(32vw, 360px) !important;
  min-width: 0 !important;
  overflow: hidden !important;
  text-overflow: ellipsis !important;
  white-space: nowrap !important;
}
[data-zimantic-toolbar] .zimantic-toolbar-home-short {
  display: none !important;
}
[data-zimantic-toolbar] .zimantic-toolbar-home.compact .zimantic-toolbar-home-full {
  display: none !important;
}
[data-zimantic-toolbar] .zimantic-toolbar-home.compact .zimantic-toolbar-home-short {
  display: inline !important;
}
[data-zimantic-toolbar] .zimantic-toolbar-article-short {
  display: none !important;
}
[data-zimantic-toolbar] .zimantic-toolbar-article.compact .zimantic-toolbar-article-full {
  display: none !important;
}
[data-zimantic-toolbar] .zimantic-toolbar-article.compact .zimantic-toolbar-article-short {
  display: inline !important;
}
[data-zimantic-toolbar] .zimantic-toolbar-breadcrumbs {
  align-items: center !important;
  display: flex !important;
  gap: 6px !important;
  min-width: 0 !important;
  overflow: hidden !important;
}
[data-zimantic-toolbar] .zimantic-toolbar-breadcrumb {
  max-width: 18vw !important;
  overflow: hidden !important;
  text-overflow: ellipsis !important;
  white-space: nowrap !important;
}
[data-zimantic-toolbar] .zimantic-toolbar-separator {
  color: #9aa3af !important;
  flex: 0 0 auto !important;
}
[data-zimantic-toolbar] .zimantic-toolbar-status {
  clip: rect(0 0 0 0) !important;
  clip-path: inset(50%) !important;
  height: 1px !important;
  margin: -1px !important;
  overflow: hidden !important;
  position: absolute !important;
  white-space: nowrap !important;
  width: 1px !important;
}
@media (prefers-color-scheme: light) {
  nav[data-zimantic-toolbar] {
    background: #fff !important;
    border-color: #dde1e7 !important;
    box-shadow: 0 1px 5px rgba(0, 0, 0, .12) !important;
    color: #1b1f24 !important;
  }
  [data-zimantic-toolbar] a,
  [data-zimantic-toolbar] button {
    border-color: #b8c0cc !important;
    color: #1f5fae !important;
  }
  [data-zimantic-toolbar] a:hover,
  [data-zimantic-toolbar] button:hover { background: #eef5ff !important; }
  [data-zimantic-toolbar] .zimantic-toolbar-separator { color: #5d6673 !important; }
}
/* Phones: a wrapped toolbar eats a big slice of an already short viewport.
   Keep it to one line that scrolls sideways instead, with finger-sized
   targets and the short labels. */
@media (max-width: 640px) {
  nav[data-zimantic-toolbar] {
    -webkit-overflow-scrolling: touch !important;
    flex-wrap: nowrap !important;
    gap: 4px !important;
    overflow-x: auto !important;
    padding:
      6px max(8px, env(safe-area-inset-right))
      6px max(8px, env(safe-area-inset-left)) !important;
    scrollbar-width: none !important;
  }
  nav[data-zimantic-toolbar]::-webkit-scrollbar { display: none !important; }
  [data-zimantic-toolbar] a,
  [data-zimantic-toolbar] button {
    min-height: 40px !important;
    padding: 8px 12px !important;
  }
  [data-zimantic-toolbar] .zimantic-toolbar-nav,
  [data-zimantic-toolbar] .zimantic-toolbar-actions {
    flex: 0 0 auto !important;
  }
  [data-zimantic-toolbar] .zimantic-toolbar-actions {
    margin-left: 8px !important;
  }
  [data-zimantic-toolbar] .zimantic-toolbar-home-full,
  [data-zimantic-toolbar] .zimantic-toolbar-article-full {
    display: none !important;
  }
  [data-zimantic-toolbar] .zimantic-toolbar-home-short,
  [data-zimantic-toolbar] .zimantic-toolbar-article-short {
    display: inline !important;
  }
}
</style>"""

ARTICLE_TOOLBAR_SCRIPT = """<script data-zimantic-toolbar>
(() => {
  const initialize = () => {
    const toolbar = document.querySelector("nav[data-zimantic-toolbar]");
    if (!toolbar) return;
    const status = toolbar.querySelector("[data-zimantic-toolbar-status]");
    const home = toolbar.querySelector("[data-zimantic-home]");
    const permalink = toolbar.querySelector("[data-zimantic-permalink]");
    const previous = toolbar.querySelector("[data-zimantic-context-previous]");
    const next = toolbar.querySelector("[data-zimantic-context-next]");
    const parentPrevious = toolbar.querySelector("[data-zimantic-parent-previous]");
    const parentNext = toolbar.querySelector("[data-zimantic-parent-next]");
    const actions = toolbar.querySelector(".zimantic-toolbar-actions");
    let selectedSearchText = "";
    const contextControls = [
      [previous, "previous"],
      [next, "next"],
    ];
    const parentControls = [
      [parentPrevious, "previous"],
      [parentNext, "next"],
    ];
    const announce = message => {
      if (status) status.textContent = message;
    };
    const setContextNavigation = navigation => {
      for (const [control, direction] of contextControls) {
        if (!control) continue;
        const url = typeof navigation?.[direction] === "string"
          ? navigation[direction]
          : "";
        const availableKey = `${direction}Available`;
        const available = typeof navigation?.[availableKey] === "boolean"
          ? navigation[availableKey]
          : Boolean(url);
        control.hidden = !navigation?.modal;
        control.disabled = !available;
        if (url) {
          control.dataset.zimanticUrl = url;
        } else {
          delete control.dataset.zimanticUrl;
        }
      }
      for (const [control, direction] of parentControls) {
        if (!control) continue;
        const url = typeof navigation?.parent?.[direction] === "string"
          ? navigation.parent[direction]
          : "";
        control.hidden = !navigation?.parent;
        control.disabled = !url;
        if (url) {
          control.dataset.zimanticUrl = url;
        } else {
          delete control.dataset.zimanticUrl;
        }
      }
    };
    window.addEventListener("message", event => {
      if (event.source !== window.parent) return;
      if (event.data?.type === "zimantic-modal-navigation") {
        setContextNavigation(event.data);
      }
    });
    // The raw article iframe cannot see or update this toolbar, so its bridge
    // asks us to navigate the whole shell. Reloading keeps breadcrumbs, the
    // permalink and the modal URL in sync, exactly like a normal page load.
    window.addEventListener("message", event => {
      if (event.data?.type !== "zimantic-shell-navigate") return;
      const url = String(event.data.url || "");
      if (url.startsWith("/content/")) location.href = url;
    });
    window.addEventListener("message", event => {
      if (event.data?.type !== "zimantic-external-link") return;
      let url;
      try {
        url = new URL(String(event.data.url || ""), location.href);
      } catch (_) {
        return;
      }
      if (url.protocol !== "http:" && url.protocol !== "https:") return;
      if (window.parent !== window) {
        window.parent.postMessage({
          type: "zimantic-external-link",
          url: url.href,
        }, "*");
      } else {
        location.href = url.href;
      }
    });
    const fitToolbarLabels = () => {
      if (!actions) return;
      for (const control of [home, permalink]) {
        if (!control) continue;
        control.classList.remove("compact");
        const available = actions.getBoundingClientRect().left -
          control.getBoundingClientRect().left - 12;
        control.classList.toggle("compact", control.scrollWidth > available);
      }
    };
    const selectedText = () => {
      const selection = typeof window.getSelection === "function"
        ? window.getSelection()
        : null;
      return (selection?.toString() || "").replace(/\\s+/g, " ").trim().slice(0, 4096);
    };
    if (home) {
      home.addEventListener("pointerdown", () => {
        selectedSearchText = selectedText();
      });
      home.addEventListener("keydown", event => {
        if (event.key === "Enter" || event.key === " ") {
          selectedSearchText = selectedText();
        }
      });
    }
    toolbar.addEventListener("click", event => {
      const control = event.target.closest("[data-zimantic-action]");
      if (!control) return;
      const action = control.dataset.zimanticAction;
      if (action === "close") {
        event.preventDefault();
        if (window.parent !== window) {
          window.parent.postMessage({type: "zimantic-article-close"}, "*");
        } else {
          window.close();
        }
      } else if (action === "home") {
        const query = selectedSearchText || selectedText();
        selectedSearchText = "";
        if (window.parent !== window) {
          event.preventDefault();
          window.parent.postMessage({type: "zimantic-search", query}, "*");
        } else if (query) {
          event.preventDefault();
          location.href = `/?q=${encodeURIComponent(query)}`;
        }
      } else if (action === "previous" || action === "next") {
        const url = control.dataset.zimanticUrl;
        if (control.disabled || window.parent === window) return;
        event.preventDefault();
        window.parent.postMessage({
          type: "zimantic-article-navigate",
          url,
          direction: action,
        }, "*");
      } else if (action === "parent-previous" || action === "parent-next") {
        const url = control.dataset.zimanticUrl;
        if (!url || control.disabled || window.parent === window) return;
        event.preventDefault();
        window.parent.postMessage({
          type: "zimantic-parent-article-navigate",
          url,
        }, "*");
      } else if (action === "copy") {
        if (
          event.button !== 0 ||
          event.ctrlKey ||
          event.metaKey ||
          event.shiftKey ||
          event.altKey
        ) {
          return;
        }
        event.preventDefault();
        if (!navigator.clipboard || !navigator.clipboard.writeText) {
          announce("Copy unavailable");
        } else {
          navigator.clipboard.writeText(location.href).then(
            () => {
              control.textContent = "Link copied";
              announce("Link copied");
            },
            () => announce("Copy unavailable")
          );
        }
      }
    });
    fitToolbarLabels();
    window.addEventListener("resize", fitToolbarLabels);
    if (window.ResizeObserver) {
      new ResizeObserver(fitToolbarLabels).observe(toolbar);
    }
    if (window.parent !== window) {
      const iconLink = document.querySelector("link[data-zimantic-article-icon]");
      window.parent.postMessage({
        type: "zimantic-article-ready",
        url: location.href,
        title: document.title,
        icon: iconLink ? iconLink.href : null,
      }, "*");
    }
  };
  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", initialize, {once: true});
  } else {
    initialize();
  }
})();
</script>"""


# Injected into raw HTML articles that predate mobile browsers. A page is
# treated as mobile-aware the moment it declares a viewport meta, and then it is
# left completely alone: its own stylesheet knows best. Only captures that never
# opted in get the reader fallback below.
ARTICLE_VIEWPORT_META = (
    '<meta name="viewport" content="width=device-width, initial-scale=1">'
)

ARTICLE_MOBILE_STYLE = """<style data-zimantic-mobile>
/* Conservative reader fallback for desktop-only captures. Most desktop pages
   anchor their layout in a fixed-width child of <body> (a 900px wrapper, say),
   so cap that child and let wide code and tables scroll inside their own box
   rather than stretching the whole document. */
@media (max-width: 800px) {
  html {
    -webkit-text-size-adjust: 100%;
    text-size-adjust: 100%;
    -webkit-overflow-scrolling: touch;
    overflow-x: auto !important;
    overflow-y: auto !important;
  }
  body {
    overflow-wrap: break-word;
    overflow-x: visible !important;
  }
  body > * { max-width: 100% !important; min-width: 0 !important; }
  img, video, iframe { max-width: 100% !important; }
  img, video { height: auto !important; }
  pre {
    max-width: 100% !important;
    overflow-x: visible !important;
  }
  table {
    display: block !important;
    max-width: 100% !important;
    overflow-x: visible !important;
  }
}
</style>"""

# A few static captures ship a responsive layout that their own JavaScript only
# enables after it detects a phone (DevDocs' `_mobile` class is the clearest
# case). A desktop-only capture never names the class, so the desktop layout
# stays collapsed on a phone. Each hint maps a markup marker to the class that
# switches the page's own mobile styles on. The class is toggled through a
# media query, so wide screens keep the layout the capture shipped with.
MOBILE_LAYOUT_HINTS = (
    ("<devdocs-navbar", "_mobile"),
)

MOBILE_LAYOUT_SCRIPT = """<script data-zimantic-mobile>
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
# has no toolbar (that lives in the /content/ shell), so this tiny bridge turns
# in-article link clicks into shell navigations. Without it, following a link
# would leave the shell's breadcrumbs and modal URL pointing at the old article.
ARTICLE_NAV_BRIDGE_SCRIPT = """<script data-zimantic-bridge>
(() => {
  const initialize = () => {
    const context = new URL(location.href);
    const parts = context.pathname.split("/").filter(Boolean);
    let zim = parts[1] || "";
    try {
      zim = decodeURIComponent(zim);
    } catch (_) {}
    const contentHome = `/content/${zim}`;
    const articleHome = `/article/${zim}`;
    const contentPrefix = `${contentHome}/`;
    const articlePrefix = `${articleHome}/`;
    document.addEventListener("click", event => {
      const anchor = event.target && event.target.closest
        ? event.target.closest("a[href]")
        : null;
      if (!anchor || !anchor.href) return;
      if (
        event.button !== 0 ||
        event.metaKey ||
        event.ctrlKey ||
        event.shiftKey ||
        event.altKey
      ) {
        return;
      }
      let url;
      try {
        url = new URL(anchor.href, context);
      } catch (_) {
        return;
      }
      if (url.protocol !== "http:" && url.protocol !== "https:") return;
      if (anchor.hasAttribute("download")) return;
      if (url.origin !== context.origin) {
        event.preventDefault();
        if (window.parent !== window) {
          window.parent.postMessage({
            type: "zimantic-external-link",
            url: url.href,
          }, "*");
        } else {
          location.href = url.href;
        }
        return;
      }
      const target = String(anchor.target || "").toLowerCase();
      if (
        target &&
        target !== "_self" &&
        target !== "_top" &&
        !(target === "_blank" && url.origin !== context.origin)
      ) {
        return;
      }
      if (event.defaultPrevented) return;
      const path = url.pathname;
      let contentPath = null;
      if (path === contentHome || path.startsWith(contentPrefix)) {
        contentPath = path;
      } else if (path === articleHome || path.startsWith(articlePrefix)) {
        contentPath = "/content" + path.slice("/article".length);
      }
      // Same-origin app pages and assets are left to the iframe.
      if (!contentPath) return;
      // Same-document fragment links must scroll in place, not reload the shell.
      if (url.pathname === context.pathname && url.search === context.search) {
        return;
      }
      event.preventDefault();
      window.parent.postMessage({
        type: "zimantic-shell-navigate",
        url: contentPath + url.search,
      }, "*");
    }, true);
  };
  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", initialize, {once: true});
  } else {
    initialize();
  }
})();
</script>"""


def _route_url(route: str, zim: str, path: str | None = None) -> str:
    url = f"/{route}/{quote_path(zim, safe='')}"
    if path:
        url += f"/{quote_path(path, safe='/')}"
    return url


def _content_url(zim: str, path: str | None = None) -> str:
    return _route_url("content", zim, path)


def _article_url(zim: str, path: str | None = None) -> str:
    return _route_url("article", zim, path)


def _article_http_error(error: Exception) -> HTTPException:
    return HTTPException(
        status_code=_ARTICLE_ERROR_STATUS[type(error)],
        detail=str(error),
    )


def _article_toolbar(zim: str, article: dict) -> str:
    zim_url = _content_url(zim)
    random_url = f"/random?zim={quote_path(zim, safe='')}"
    zim_name = html_escape(str(zim), quote=True)
    article_path = str(article.get("path") or "").strip("/")
    path_parts = [part for part in article_path.split("/") if part]
    permalink_url = _content_url(zim, article_path) if article_path else zim_url
    article_title = html_escape(
        str(article.get("title") or article_path or "Permalink"),
        quote=False,
    )
    breadcrumbs = []
    for depth in range(1, len(path_parts)):
        parent_path = "/".join(path_parts[:depth])
        parent_url = _content_url(zim, parent_path)
        breadcrumbs.append(
            f'<span class="zimantic-toolbar-separator" aria-hidden="true">›</span>'
            f'<a class="zimantic-toolbar-breadcrumb" href="{parent_url}" '
            f'title="Open {html_escape(parent_path, quote=True)}">'
            f'{html_escape(path_parts[depth - 1])}</a>'
        )
    breadcrumb_html = "".join(breadcrumbs) + (
        '<span class="zimantic-toolbar-separator" aria-hidden="true">›</span>'
        f'<a class="zimantic-toolbar-article" data-zimantic-permalink '
        f'data-zimantic-action="copy" href="{permalink_url}" '
        f'title="Open or copy this article\'s permalink">'
        f'<span class="zimantic-toolbar-article-full">{article_title}</span>'
        '<span class="zimantic-toolbar-article-short">'
        "Permalink</span></a>"
    )
    return f"""<nav data-zimantic-toolbar aria-label="Article navigation">
  <div class="zimantic-toolbar-nav">
    <button type="button" data-zimantic-action="close" title="Close article">Close</button>
    <span class="zimantic-toolbar-context-nav" aria-label="Adjacent search results">
      <button type="button" data-zimantic-action="previous"
        data-zimantic-context-previous hidden disabled aria-label="Previous search result"
        title="Open the previous search result">←</button>
      <button type="button" data-zimantic-action="next"
        data-zimantic-context-next hidden disabled aria-label="Next search result"
        title="Open the next search result">→</button>
    </span>
    <a href="{random_url}" title="Open a random article">Random</a>
    <a class="zimantic-toolbar-home" data-zimantic-home href="{zim_url}" title="Open this ZIM's homepage">
      <span class="zimantic-toolbar-home-full">{zim_name}</span>
      <span class="zimantic-toolbar-home-short">Home</span>
    </a>
    <span class="zimantic-toolbar-breadcrumbs">{breadcrumb_html}</span>
  </div>
  <div class="zimantic-toolbar-actions">
    <span class="zimantic-toolbar-parent-nav-group" aria-label="Adjacent links in parent page">
      <button type="button" class="zimantic-toolbar-parent-nav"
        data-zimantic-action="parent-previous" data-zimantic-parent-previous
        hidden disabled aria-label="Previous link in parent page"
        title="Open the previous link in the parent page">←</button>
      <button type="button" class="zimantic-toolbar-parent-nav"
        data-zimantic-action="parent-next" data-zimantic-parent-next
        hidden disabled aria-label="Next link in parent page"
        title="Open the next link in the parent page">→</button>
    </span>
    <a href="/" data-zimantic-action="home" title="Search your library">Zimantic</a>
  </div>
  <span class="zimantic-toolbar-status" data-zimantic-toolbar-status aria-live="polite"></span>
</nav>"""


def _decorate_article_bridge(content: bytes | memoryview, zim: str) -> bytes:
    """Add the shell-navigation bridge and mobile fallback to a raw article.

    The article keeps its own markup; a small script lets link clicks ask the
    /content/ shell to navigate, and desktop-only captures also get the mobile
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
    additions = mobile_head + ARTICLE_NAV_BRIDGE_SCRIPT
    head_close = re.search(r"</head\s*>", document, re.IGNORECASE)
    if head_close:
        position = head_close.start()
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
    content: bytes | memoryview, zim: str, path: str | None
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

    base = _article_url(zim, path) if path else _article_url(zim) + "/"
    icon = None
    for tag in re.findall(r"<link\b[^>]*>", head, re.IGNORECASE):
        if "icon" not in _html_attribute(tag, "rel").casefold():
            continue
        href = _html_attribute(tag, "href").strip()
        if not href:
            continue
        parsed = urlsplit(urljoin(base, href))
        if parsed.scheme or parsed.netloc or not parsed.path.startswith("/article/"):
            continue
        icon = parsed.path + (f"?{parsed.query}" if parsed.query else "")
        break
    return title, icon


def _content_shell(
    zim: str,
    article: dict,
    status_code: int = 200,
    fallback_icon: str | None = None,
) -> Response:
    """Toolbar shell page whose iframe loads the raw article from /article/.

    Keeping the toolbar in this outer document means it survives any
    navigation inside the iframe, including 404s and other error bodies. The
    shell also mirrors the article's title and favicon: directly loaded shells
    use them for the browser tab, and embedded shells forward them to the host
    so the modal's tab follows the article too.
    """
    article_path = str(article.get("path") or "").strip("/")
    iframe_url = _article_url(zim, article_path) if article_path else _article_url(zim)
    toolbar = _article_toolbar(zim, article)
    entry_title = str(article.get("title") or article_path or zim)
    parsed_title = None
    icon = fallback_icon
    if article.get("mimetype") == "text/html" and article.get("content") is not None:
        parsed_title, parsed_icon = _article_head_meta(
            article["content"], zim, article_path or None
        )
        icon = parsed_icon or fallback_icon
    title = html_escape(parsed_title or entry_title, quote=False)
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
        + ARTICLE_TOOLBAR_STYLE
        + """<style data-zimantic-shell>
html, body {
  height: 100%;
  margin: 0;
  max-width: 100%;
  overflow: hidden;
}
body {
  background: #fff;
  display: flex;
  flex-direction: column;
}
nav[data-zimantic-toolbar] {
  flex: 0 0 auto !important;
  margin: 0 !important;
  position: static !important;
}
iframe[data-zimantic-article] {
  background: #fff;
  border: 0;
  display: block;
  flex: 1 1 auto !important;
  min-height: 0 !important;
  min-width: 0 !important;
  width: 100% !important;
}
</style>
</head>
<body>
"""
        + toolbar
        + f'\n<iframe data-zimantic-article title="{title}" '
        f'src="{iframe_url}" referrerpolicy="no-referrer"></iframe>\n'
        + ARTICLE_TOOLBAR_SCRIPT
        + "\n</body>\n</html>"
    )
    return Response(
        content=html.encode("utf-8"),
        media_type="text/html",
        status_code=status_code,
        headers={"Cache-Control": "no-cache"},
    )


def _article_content_response(content: bytes | memoryview, mimetype: str) -> Response:
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
            headers={
                "Cache-Control": "no-cache",
                "Content-Length": str(len(content)),
                **ARTICLE_CORS_HEADERS,
            },
        )
    return Response(
        content=content,
        media_type=mimetype,
        headers={"Cache-Control": "no-cache", **ARTICLE_CORS_HEADERS},
    )


def _json_with_etag(request: Request, payload) -> Response:
    """JSON response with a content ETag, so a warm browser can revalidate
    source discovery cheaply with If-None-Match instead of re-downloading."""
    body = json.dumps(payload, separators=(",", ":"), sort_keys=True, default=str)
    etag = '"' + hashlib.sha1(body.encode("utf-8")).hexdigest() + '"'
    headers = {"ETag": etag, "Cache-Control": "no-cache"}
    if request.headers.get("if-none-match") == etag:
        return Response(status_code=304, headers=headers)
    return Response(content=body, media_type="application/json", headers=headers)


def create_app(searchClass, debug: bool = False) -> FastAPI:
    app = FastAPI(title="zimantic")
    app.add_middleware(GZipMiddleware, minimum_size=GZIP_MIN_SIZE)
    from .routes import register_routes

    register_routes(app, searchClass, debug)
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
            logger.error("zimantic: reload failed: %s", error)
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


def serve(searchClass, port: int, debug: bool = False) -> None:
    app = create_app(searchClass, debug=debug)
    try:
        PID_FILE.write_text(f"{os.getpid()}\n", encoding="utf-8")
    except OSError as error:
        logger.error("zimantic: could not write PID file %s: %s", PID_FILE, error)

    # Optional Kiwix catalog discovery runs in the background so an unreachable
    # Kiwix server cannot delay binding the port.
    start_catalog = getattr(searchClass, "start_catalog_refresh", None)
    if callable(start_catalog):
        start_catalog()

    sighup = getattr(signal, "SIGHUP", None)
    if sighup is not None:
        if debug:
            signal.signal(sighup, _handle_restart_request)
        else:
            guard = threading.Lock()
            signal.signal(
                sighup,
                lambda _signum, _frame: _handle_reload_request(searchClass, guard),
            )

    server_mode = " (debug)" if debug else ""
    logger.info(
        f"zimantic: server{server_mode} loaded at http://localhost:{port}\n"
        f"zimantic: PID {os.getpid()}; reload with `zimantic reload` "
    )
    start_faiss = getattr(searchClass, "start_faiss", None)
    if callable(start_faiss):
        start_faiss()
    try:
        uvicorn.run(app, host="0.0.0.0", port=port, log_level="warning")
    finally:
        try:
            PID_FILE.unlink()
        except OSError:
            pass

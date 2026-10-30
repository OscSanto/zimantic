import hashlib
import json
import logging
import os
import re
import signal
import sys
import threading
from html import escape as html_escape
from html import unescape as html_unescape
from pathlib import Path
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
from ..settings import normalize_web_base_path

ASSET_DIR = Path(__file__).parent / "static"
PAGE = ASSET_DIR / "index.html"
APP_CSS = ASSET_DIR / "app.css"
APP_JS = ASSET_DIR / "app.js"
INDEXEDDB_SHIM = ASSET_DIR / "indexeddb-shim.js"
FAVICON = ASSET_DIR / "favicon.ico"
FAVICON_16 = ASSET_DIR / "favicon-16x16.png"
FAVICON_32 = ASSET_DIR / "favicon-32x32.png"
APPLE_TOUCH_ICON = ASSET_DIR / "apple-touch-icon.png"
ANDROID_CHROME_192 = ASSET_DIR / "android-chrome-192x192.png"
ANDROID_CHROME_512 = ASSET_DIR / "android-chrome-512x512.png"
WEB_MANIFEST = ASSET_DIR / "site.webmanifest"
PID_FILE = Path("zimantic.pid")
logger = logging.getLogger(__name__)

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
# explicit CORS access to the public raw-entry route.
ARTICLE_CORS_HEADERS = {"Access-Control-Allow-Origin": "*"}
# Both HTML flavors receive the article bridge, mobile fallback, and head
# metadata handling.
HTML_MIMETYPES = frozenset({"text/html", "application/xhtml+xml"})

# Some JavaScript applications read ``document.cookie`` even when they do not
# use cookies. The browser throws for that getter in the opaque-origin article
# sandbox; expose the honest empty value so those applications can start.
ARTICLE_COOKIE_SHIM = """<script data-zimantic-cookie-shim="true">
(() => {
  try {
    void document.cookie;
  } catch (_) {
    try {
      Object.defineProperty(document, "cookie", {
        configurable: true,
        get: () => "",
        set: () => {},
      });
    } catch (_) {}
  }
})();
</script>"""


# Web Storage is unavailable in the opaque-origin article sandbox used by the
# modal. Read access throws a SecurityError, which aborts the application that
# owns the page (freeCodeCamp's Pinia store is one example). Keep the page
# running with an in-memory store and tell the shell, so it can offer the direct
# /content/ view where the browser's real storage is available.
ARTICLE_STORAGE_SHIM = """<script data-zimantic-storage-shim="true">
(() => {
  const makeMemoryStorage = () => {
    const values = new Map();
    const api = {
      getItem: key => values.has(String(key)) ? values.get(String(key)) : null,
      setItem: (key, value) => { values.set(String(key), String(value)); },
      removeItem: key => { values.delete(String(key)); },
      clear: () => { values.clear(); },
      key: index => [...values.keys()][index] ?? null,
    };
    return new Proxy(api, {
      get(target, property) {
        if (property === "length") return values.size;
        if (property in target) return target[property];
        if (typeof property === "string" && values.has(property)) {
          return values.get(property);
        }
        return undefined;
      },
      set(target, property, value) {
        if (typeof property !== "string" || property in target) return false;
        values.set(property, String(value));
        return true;
      },
      deleteProperty(target, property) {
        values.delete(String(property));
        return true;
      },
      has(target, property) {
        return property in target || values.has(String(property));
      },
      ownKeys() {
        return [...values.keys()];
      },
      getOwnPropertyDescriptor(target, property) {
        if (!values.has(String(property))) return undefined;
        return {
          configurable: true,
          enumerable: true,
          value: values.get(String(property)),
          writable: true,
        };
      },
    });
  };
  const report = name => {
    try {
      if (window.parent !== window) {
        window.parent.postMessage({
          type: "zimantic-article-storage-blocked",
          storage: name,
        }, "*");
      }
    } catch (_) {}
  };
  for (const name of ["localStorage", "sessionStorage"]) {
    let descriptor = Object.getOwnPropertyDescriptor(window, name);
    let prototype = Object.getPrototypeOf(window);
    while (!descriptor && prototype) {
      descriptor = Object.getOwnPropertyDescriptor(prototype, name);
      prototype = Object.getPrototypeOf(prototype);
    }
    let blocked = false;
    try {
      if (descriptor && typeof descriptor.get === "function") {
        descriptor.get.call(window);
      } else {
        void window[name];
      }
    } catch (_) {
      blocked = true;
    }
    if (!blocked) continue;
    let storage = null;
    let reported = false;
    try {
      Object.defineProperty(window, name, {
        configurable: true,
        enumerable: true,
        get: () => {
          if (!storage) storage = makeMemoryStorage();
          if (!reported) {
            reported = true;
            report(name);
          }
          return storage;
        },
        set: () => {},
      });
    } catch (_) {}
  }
})();
</script>"""


ARTICLE_INDEXEDDB_LOADER = """<script data-zimantic-indexeddb-loader="true">
(() => {
  // The modal's sandbox has an opaque origin, where IndexedDB is unavailable.
  // Load the in-memory polyfill synchronously so page scripts see it.
  if (self.origin !== "null") return;
  const url = %s;
  document.write(
    '<script src="' + url + '" data-zimantic-indexeddb-shim="true"><' + '/script>'
  );
})();
</script>"""


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
[data-zimantic-toolbar] .zimantic-toolbar-context-nav button {
  font-size: 18px !important;
  line-height: 1 !important;
  min-width: 32px !important;
  padding: 4px 7px !important;
}
[data-zimantic-toolbar] .zimantic-toolbar-parent-nav {
  flex: 0 0 auto !important;
  font-size: 15px !important;
  height: 30px !important;
  line-height: 1 !important;
  min-width: 30px !important;
  padding: 4px !important;
  width: 30px !important;
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
  [data-zimantic-toolbar] .zimantic-toolbar-actions > .zimantic-toolbar-search {
    display: none !important;
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
  const appBasePath = %s;
  const storageNotice = document.querySelector("[data-zimantic-storage-notice]");
  const storageNoticeOpen = document.querySelector(
    "[data-zimantic-storage-notice-open]"
  );
  const storageNoticeDismiss = document.querySelector(
    "[data-zimantic-storage-notice-dismiss]"
  );
  let storageNoticeDismissed = false;
  const showStorageNotice = () => {
    if (storageNoticeDismissed || !storageNotice) return;
    if (storageNoticeOpen) storageNoticeOpen.href = location.href;
    storageNotice.hidden = false;
  };
  if (storageNoticeDismiss) {
    storageNoticeDismiss.addEventListener("click", () => {
      storageNoticeDismissed = true;
      if (storageNotice) storageNotice.hidden = true;
      const articleFrame = document.querySelector("iframe[data-zimantic-article]");
      if (articleFrame) articleFrame.focus();
    });
  }
  if (storageNoticeOpen) {
    // Ask the host to open the standalone view so it does not depend on the
    // modal iframe's sandbox popup permissions.
    storageNoticeOpen.addEventListener("click", event => {
      if (window.parent === window) return;
      event.preventDefault();
      window.parent.postMessage({
        type: "zimantic-open-article",
        url: location.href,
      }, "*");
    });
  }
  // Error shells are shown inside the modal too. A normal link there would
  // navigate the sandboxed shell to the app, which cannot fetch its config in
  // an opaque-origin frame. Let the host close the modal instead.
  document.addEventListener("click", event => {
    const target = event.target instanceof Element
      ? event.target.closest("[data-zimantic-search-home]")
      : null;
    if (!target || window.parent === window) return;
    if (event.button !== 0 || event.ctrlKey || event.metaKey || event.shiftKey || event.altKey) {
      return;
    }
    event.preventDefault();
    window.parent.postMessage({type: "zimantic-search", query: ""}, "*");
  });
  // Register before DOMContentLoaded: the article frame can report blocked
  // storage while its own deferred modules run, before this shell initializes.
  window.addEventListener("message", event => {
    if (event.data?.type !== "zimantic-article-storage-blocked") return;
    showStorageNotice();
  });
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
    const articleFrame = document.querySelector("iframe[data-zimantic-article]");
    const shellUrl = new URL(location.href);
    const localPath = pathname => {
      if (!appBasePath) return pathname;
      if (pathname === appBasePath) return "/";
      return pathname.startsWith(`${appBasePath}/`)
        ? pathname.slice(appBasePath.length)
        : null;
    };
    const shellPath = localPath(shellUrl.pathname) || "";
    const articleUrl = new URL(shellUrl.href);
    articleUrl.pathname = `${appBasePath}/article${shellPath.slice("/content".length)}`;
    const permalinkLabel = permalink ? permalink.innerHTML : "";
    let permalinkResetTimer = null;
    let selectedSearchText = "";
    let articleSelection = "";
    let offlineRequest = 0;
    const normalizeSelection = value =>
      String(value || "").replace(/\\s+/g, " ").trim().slice(0, 2048);
    const shareUrl = value => {
      const url = new URL(location.href);
      url.hash = "";
      url.searchParams.delete("highlight");
      const text = normalizeSelection(value);
      if (text) url.searchParams.set("highlight", text);
      return url.href;
    };
    const setArticleSelection = value => {
      articleSelection = normalizeSelection(value);
      if (permalink) permalink.href = shareUrl(articleSelection);
      if (window.parent !== window) {
        window.parent.postMessage({
          type: "zimantic-article-selection",
          url: shareUrl(articleSelection),
        }, "*");
      } else {
        history.replaceState(history.state, "", shareUrl(articleSelection));
      }
    };
    const scrollArticleTo = hash => {
      const value = String(hash || "");
      if (!value || !articleFrame || !articleFrame.contentWindow) return;
      articleFrame.contentWindow.postMessage({
        type: "zimantic-article-hash",
        hash: value,
      }, "*");
    };
    if (articleFrame) {
      articleFrame.addEventListener("load", () => {
        if (location.hash) scrollArticleTo(location.hash);
      });
    }
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
    const showPermalinkCopied = () => {
      if (!permalink) return;
      clearTimeout(permalinkResetTimer);
      permalink.textContent = "Link copied";
      permalinkResetTimer = setTimeout(() => {
        permalinkResetTimer = null;
        if (permalink && permalink.isConnected) {
          permalink.innerHTML = permalinkLabel;
        }
      }, 1600);
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
        return;
      }
      if (event.data?.type === "zimantic-article-copy-result") {
        if (event.data.ok) {
          showPermalinkCopied();
          announce("Link copied");
        } else {
          announce("Copy unavailable");
        }
      }
    });
    document.addEventListener("keydown", event => {
      if (event.key !== "Escape" || window.parent === window) return;
      event.preventDefault();
      window.parent.postMessage({type: "zimantic-article-close"}, "*");
    });
    window.addEventListener("message", event => {
      if (!articleFrame || event.source !== articleFrame.contentWindow) return;
      if (event.data?.type === "zimantic-article-close") {
        if (window.parent !== window) {
          window.parent.postMessage({type: "zimantic-article-close"}, "*");
        }
        return;
      }
      if (event.data?.type === "zimantic-article-ready" && location.hash) {
        scrollArticleTo(location.hash);
        return;
      }
      if (event.data?.type !== "zimantic-article-selection") return;
      setArticleSelection(event.data.text);
    });
    // The raw article iframe cannot see or update this toolbar, so its bridge
    // asks us to navigate the whole shell. Reloading keeps breadcrumbs, the
    // permalink and the modal URL in sync, exactly like a normal page load.
    const contentUrl = value => {
      let target;
      try {
        target = new URL(String(value || ""), location.href);
      } catch (_) {
        return null;
      }
      // The shell is loaded with an opaque origin in the modal, so compare
      // parsed URLs instead of the null `location.origin`.
      if (target.origin !== shellUrl.origin) return null;
      const path = localPath(target.pathname);
      if (path !== "/content" && !(path && path.startsWith("/content/"))) {
        return null;
      }
      return target;
    };
    window.addEventListener("message", event => {
      if (event.data?.type !== "zimantic-shell-navigate") return;
      const target = contentUrl(event.data.url);
      if (!target) return;
      offlineRequest += 1;
      if (window.parent !== window) {
        location.replace(target.href);
      } else {
        location.href = target.href;
      }
    });
    // A directly loaded /content/ view should make the same choice as the
    // modal: use the captured copy when it exists, and only go online when the
    // article is not in the ZIM.
    const openOfflineArticle = (target, fallbackUrl) => {
      if (
        target.pathname === location.pathname &&
        (target.search === location.search ||
          (target.hash && !target.search))
      ) {
        scrollArticleTo(target.hash);
        return;
      }
      const request = ++offlineRequest;
      const hash = target.hash;
      const probe = new URL(target.href);
      probe.hash = "";
      fetch(probe.href, {cache: "no-store", credentials: "same-origin"})
        .then(response => {
          if (request !== offlineRequest) return;
          if (!response.ok) throw new Error(`HTTP ${response.status}`);
          const resolved = new URL(response.url || probe.href);
          if (resolved.origin !== shellUrl.origin) throw new Error("moved");
          resolved.search = target.search;
          resolved.hash = hash;
          location.href = resolved.href;
        })
        .catch(() => {
          if (request !== offlineRequest) return;
          location.href = fallbackUrl;
        });
    };
    // The raw article reports the link it saw, not what it means. Resolve and
    // classify every article link here so wombat and other replay shims cannot
    // turn authored relative links into live-site URLs: the raw `href`
    // attribute and the article's local /article/ URL are the source of truth.
    const articleRoute = value => {
      let target;
      try {
        target = new URL(String(value || ""), location.href);
      } catch (_) {
        return null;
      }
      if (target.origin !== shellUrl.origin) return null;
      const path = localPath(target.pathname);
      if (!path || (path !== "/article" && !path.startsWith("/article/"))) {
        return null;
      }
      const parts = path.split("/").filter(Boolean);
      return {url: target, path, zim: parts[1] || "", root: parts[2] || ""};
    };
    const contentPathFor = url => {
      const path = localPath(url.pathname);
      if (path === "/content" || (path && path.startsWith("/content/"))) {
        return url.pathname;
      }
      if (path === "/article" || (path && path.startsWith("/article/"))) {
        return `${appBasePath}/content${path.slice("/article".length)}`;
      }
      return null;
    };
    const comparableHost = value =>
      String(value || "").toLowerCase().replace(/\\.$/, "").replace(/^www\\./, "");
    const offlineUrlFor = (url, route) => {
      let root = route.root;
      try {
        root = decodeURIComponent(root);
      } catch (_) {}
      if (!root || comparableHost(root) !== comparableHost(url.hostname)) {
        return null;
      }
      const local = new URL(route.url.href);
      local.pathname = `${appBasePath}/content/${route.zim}/` +
        `${encodeURIComponent(root)}/${url.pathname.replace(/^\\/+/, "")}`;
      local.search = url.search;
      local.hash = url.hash;
      return local.href;
    };
    const currentContentUrl = route => {
      const path = contentPathFor(route.url);
      return path ? path + route.url.search : null;
    };
    const handleArticleLink = data => {
      const route = articleRoute(data?.url) || articleRoute(articleUrl.href);
      if (!route) return;
      const href = String(data?.href || "");
      if (!href) return;
      let url;
      try {
        const base = String(data?.base || "");
        url = new URL(href, base ? new URL(base, route.url) : route.url);
      } catch (_) {
        return;
      }
      if (url.protocol !== "http:" && url.protocol !== "https:") return;
      // A same-document link keeps the iframe's scroll position.
      if (url.pathname === route.url.pathname && url.search === route.url.search) {
        if (url.hash) scrollArticleTo(url.hash);
        return;
      }
      const contentPath = contentPathFor(url);
      if (url.origin === shellUrl.origin && contentPath) {
        const target = new URL(url.href);
        target.pathname = contentPath;
        if (window.parent !== window) {
          window.parent.postMessage({
            type: "zimantic-article-follow",
            url: target.href,
            parent: currentContentUrl(route),
          }, "*");
          return;
        }
        offlineRequest += 1;
        location.href = target.href;
        return;
      }
      // Same-host links that are not article or shell pages belong to the app.
      if (url.origin === shellUrl.origin) {
        location.href = url.href;
        return;
      }
      const offlineUrl = offlineUrlFor(url, route);
      if (window.parent !== window) {
        window.parent.postMessage({
          type: "zimantic-external-link",
          url: url.href,
          offlineUrl,
          parent: currentContentUrl(route),
        }, "*");
        return;
      }
      if (offlineUrl) {
        openOfflineArticle(new URL(offlineUrl), url.href);
        return;
      }
      location.href = url.href;
    };
    window.addEventListener("message", event => {
      if (!articleFrame || event.source !== articleFrame.contentWindow) return;
      if (event.data?.type !== "zimantic-article-link") return;
      handleArticleLink(event.data);
    });
    // A capture can finish loading with nothing to show: a JavaScript shell that
    // renders content it never saved, a redirect to a page whose assets 404, or
    // a parked domain. An empty iframe looks broken, so explain it in the shell.
    const captureNotice = document.querySelector("[data-zimantic-capture-notice]");
    const captureNoticeDismiss = document.querySelector(
      "[data-zimantic-capture-notice-dismiss]"
    );
    let captureNoticeTimer = null;
    let captureNoticeDismissed = false;
    const captureHasVisibleContent = () => {
      if (!articleFrame) return true;
      let frameDocument = null;
      try {
        frameDocument = articleFrame.contentDocument;
      } catch (_) {
        return true;
      }
      // A cross-origin document is not ours to judge.
      if (!frameDocument || !frameDocument.body) return true;
      const body = frameDocument.body;
      if (String(body.innerText || "").trim()) return true;
      const media = body.querySelectorAll(
        "img, svg, canvas, video, audio, iframe, object, embed, " +
        "input, textarea, select"
      );
      for (const node of media) {
        const rect = node.getBoundingClientRect();
        if (rect.width < 8 || rect.height < 8) continue;
        if (node.tagName === "IMG" && !node.naturalWidth) continue;
        return true;
      }
      return false;
    };
    const hideCaptureNotice = () => {
      clearTimeout(captureNoticeTimer);
      captureNoticeTimer = null;
      if (captureNotice) captureNotice.hidden = true;
    };
    const scheduleCaptureCheck = () => {
      clearTimeout(captureNoticeTimer);
      if (!captureNotice || captureNoticeDismissed) return;
      let attempts = 0;
      const check = () => {
        attempts += 1;
        if (captureHasVisibleContent()) {
          hideCaptureNotice();
          return;
        }
        if (attempts >= 3) {
          captureNotice.hidden = false;
          return;
        }
        // Give a script-rendered page a moment to paint before deciding.
        captureNoticeTimer = setTimeout(check, attempts === 1 ? 700 : 1500);
      };
      captureNoticeTimer = setTimeout(check, 400);
    };
    if (captureNoticeDismiss) {
      captureNoticeDismiss.addEventListener("click", () => {
        captureNoticeDismissed = true;
        hideCaptureNotice();
      });
    }
    if (articleFrame) {
      // Every navigation in the frame fires load, including a page's own
      // script redirect, so a blank redirect target still gets an explanation.
      articleFrame.addEventListener("load", scheduleCaptureCheck);
    }
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
          // Browsers only let scripts close windows they opened. When that
          // is refused in a directly loaded /content/ view, leave the view:
          // go back if this tab came from another page, or open the library
          // when the article is the first history entry.
          window.close();
          setTimeout(() => {
            if (window.closed) return;
            if (history.length > 1) {
              history.back();
            } else {
              location.href = `${appBasePath}/`;
            }
          }, 0);
        }
      } else if (action === "home") {
        const query = selectedSearchText || articleSelection || selectedText();
        selectedSearchText = "";
        if (window.parent !== window) {
          event.preventDefault();
          window.parent.postMessage({type: "zimantic-search", query}, "*");
        } else if (query) {
          event.preventDefault();
          location.href = `${appBasePath}/?q=${encodeURIComponent(query)}`;
        }
      } else if (action === "random") {
        // A plain click follows the href (random article in this ZIM).
        // Alt-click opens the homepage of a random available ZIM instead.
        if (!event.altKey) return;
        event.preventDefault();
        const randomZimUrl = control.dataset.zimanticRandomZimUrl;
        if (randomZimUrl) location.href = randomZimUrl;
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
        const text = shareUrl(articleSelection);
        const copied = () => {
          showPermalinkCopied();
          announce("Link copied");
        };
        const delegate = () => {
          announce("Copying…");
          window.parent.postMessage({
            type: "zimantic-article-copy",
            text,
          }, "*");
        };
        if (window.parent !== window) {
          // Clipboard access is blocked in the modal's sandbox (notably in
          // Chromium), so let the top-level page perform the write.
          delegate();
        } else if (!navigator.clipboard || !navigator.clipboard.writeText) {
          announce("Copy unavailable");
        } else {
          navigator.clipboard.writeText(text).then(
            copied,
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
    '<meta name="viewport" content="width=device-width, initial-scale=1" />'
)

ARTICLE_MOBILE_STYLE = """<style data-zimantic-mobile="true">
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
# has no toolbar and does no URL classification: this thin bridge reports the
# authored href to the /content/ shell, which owns navigation, offline-link
# mapping and the external-link notice. A top-level /article/ visit hands off
# to the shell so both routes offer one browsing experience.
ARTICLE_NAV_BRIDGE_SCRIPT = """<script data-zimantic-bridge="true">
(() => {
  const prefix = %s;
  const context = new URL(location.href);
  const routePath = prefix && context.pathname.startsWith(`${prefix}/`)
    ? context.pathname.slice(prefix.length)
    : context.pathname;
  // /article/ is the raw entry source for the /content/ shell, not a second
  // browsing surface. A top-level visit hands off to the shell so both routes
  // keep the same toolbar, URL and navigation behavior. The shell's own iframe
  // has a parent window and stays here.
  if (
    window.parent === window &&
    (routePath === "/article" || routePath.startsWith("/article/"))
  ) {
    const shell = new URL(context.href);
    shell.pathname = `${prefix}/content${routePath.slice("/article".length)}`;
    location.replace(shell.href);
    return;
  }
  const initialize = () => {
    const normalizeText = value =>
      String(value || "").replace(/\\s+/g, " ").trim();
    const escapeRegExp = value =>
      String(value).replace(/[.*+?^${}()|[\\]\\\\]/g, "\\\\$&");
    const phrasePattern = words =>
      new RegExp(words.map(escapeRegExp).join("\\\\s+"), "i");
    const visibleTextNodes = () => {
      const root = document.body || document.documentElement;
      if (!root) return [];
      const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT, {
        acceptNode(node) {
          if (!node.nodeValue || !node.nodeValue.trim()) {
            return NodeFilter.FILTER_REJECT;
          }
          const parent = node.parentElement;
          if (
            !parent ||
            parent.closest("script, style, noscript, template, textarea, select, option")
          ) {
            return NodeFilter.FILTER_REJECT;
          }
          return NodeFilter.FILTER_ACCEPT;
        },
      });
      const nodes = [];
      let node = walker.nextNode();
      while (node) {
        nodes.push(node);
        node = walker.nextNode();
      }
      return nodes;
    };
    const matchWords = (nodes, words, fromIndex = 0, fromOffset = 0) => {
      const limit = Math.min(words.length, 24);
      for (let take = limit; take >= 1; take--) {
        const pattern = phrasePattern(words.slice(0, take));
        for (let index = fromIndex; index < nodes.length; index++) {
          const value = nodes[index].nodeValue || "";
          const offset = index === fromIndex ? fromOffset : 0;
          const tail = offset ? value.slice(offset) : value;
          pattern.lastIndex = 0;
          const match = pattern.exec(tail);
          if (!match) continue;
          return {
            index,
            start: offset + match.index,
            end: offset + match.index + match[0].length,
            wordsUsed: take,
          };
        }
      }
      return null;
    };
    const matchLastWords = (nodes, words, after) => {
      const limit = Math.min(words.length, 24);
      for (let take = limit; take >= 1; take--) {
        const pattern = phrasePattern(words.slice(words.length - take));
        for (let index = after.index; index < nodes.length; index++) {
          const value = nodes[index].nodeValue || "";
          const offset = index === after.index ? after.end : 0;
          const tail = offset ? value.slice(offset) : value;
          pattern.lastIndex = 0;
          const match = pattern.exec(tail);
          if (!match) continue;
          return {
            index,
            start: offset + match.index,
            end: offset + match.index + match[0].length,
          };
        }
      }
      return null;
    };
    const applyHighlight = value => {
      const text = normalizeText(value).slice(0, 4096);
      if (!text) return false;
      const words = text.split(" ");
      const nodes = visibleTextNodes();
      if (!nodes.length) return false;
      const start = matchWords(nodes, words);
      if (!start) return false;
      const end = start.wordsUsed === words.length
        ? start
        : matchLastWords(nodes, words, start) || start;
      const range = document.createRange();
      range.setStart(nodes[start.index], start.start);
      range.setEnd(nodes[end.index], end.end);
      if (range.collapsed) return false;
      const selection = window.getSelection();
      if (!selection) return false;
      selection.removeAllRanges();
      selection.addRange(range);
      const target = range.startContainer.parentElement || document.body;
      if (target && target.scrollIntoView) {
        target.scrollIntoView({block: "center", inline: "nearest"});
      }
      return true;
    };
    let lastSelection = "";
    let selectionTimer = null;
    const currentSelection = () => {
      const selection = typeof window.getSelection === "function"
        ? window.getSelection()
        : null;
      return normalizeText(selection?.toString() || "").slice(0, 2048);
    };
    const publishSelection = () => {
      const text = currentSelection();
      if (text === lastSelection) return;
      lastSelection = text;
      if (window.parent !== window) {
        window.parent.postMessage({
          type: "zimantic-article-selection",
          text,
        }, "*");
      }
    };
    const scheduleSelection = () => {
      clearTimeout(selectionTimer);
      selectionTimer = setTimeout(publishSelection, 200);
    };
    document.addEventListener("selectionchange", scheduleSelection);
    // Publish on the gesture that finishes a selection too, so a quick click on
    // Copy link cannot race the debounce and drop the selected text.
    window.addEventListener("mouseup", publishSelection);
    window.addEventListener("keyup", publishSelection);
    const fromParent = event => {
      // Wombat wraps window.parent in a proxy on zimit captures, so a strict
      // reference comparison with event.source drops real parent messages.
      // These messages only scroll and highlight the article, so accepting
      // them by type is preferable to losing them behind the proxy.
      return event.source !== window;
    };
    window.addEventListener("message", event => {
      if (!fromParent(event)) return;
      if (event.data?.type !== "zimantic-article-highlight") return;
      applyHighlight(event.data.text);
    });
    window.addEventListener("message", event => {
      if (!fromParent(event)) return;
      if (event.data?.type !== "zimantic-article-hash") return;
      const hash = String(event.data.hash || "");
      if (!hash) return;
      try {
        location.hash = hash;
      } catch (_) {}
    });
    if (window.parent !== window) {
      document.addEventListener("keydown", event => {
        if (event.key !== "Escape") return;
        window.parent.postMessage({type: "zimantic-article-close"}, "*");
      });
    }
    const requestedHighlight = (() => {
      try {
        return normalizeText(
          new URL(location.href).searchParams.get("highlight") || ""
        ).slice(0, 4096);
      } catch (_) {
        return "";
      }
    })();
    if (requestedHighlight) {
      let attempts = 0;
      const tryHighlight = () => {
        if (applyHighlight(requestedHighlight) || attempts >= 6) return;
        attempts += 1;
        setTimeout(tryHighlight, 400);
      };
      tryHighlight();
    }
    document.addEventListener("click", event => {
      const anchor = event.target && event.target.closest
        ? event.target.closest("a[href]")
        : null;
      if (!anchor) return;
      // Wombat (zimit) rewrites the `href` property to the live site URL.
      // Report the authored attribute and let the /content/ shell decide what
      // it means: another offline entry, the app, or the live web.
      const href = anchor.getAttribute("href");
      if (!href || href.startsWith("#")) return;
      if (
        event.button !== 0 ||
        event.metaKey ||
        event.ctrlKey ||
        event.shiftKey ||
        event.altKey
      ) {
        return;
      }
      if (anchor.hasAttribute("download")) return;
      let url;
      try {
        url = new URL(href, context);
      } catch (_) {
        return;
      }
      if (url.protocol !== "http:" && url.protocol !== "https:") return;
      const target = String(anchor.target || "").toLowerCase();
      if (target && target !== "_self" && target !== "_top") return;
      // Same-document fragment links must scroll in place, not reload the shell.
      if (url.pathname === context.pathname && url.search === context.search) {
        return;
      }
      if (event.defaultPrevented) return;
      const baseTag = document.querySelector("base[href]");
      event.preventDefault();
      window.parent.postMessage({
        type: "zimantic-article-link",
        href,
        base: baseTag ? String(baseTag.getAttribute("href") || "") : "",
        url: context.href,
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


def _route_url(
    route: str, zim: str, path: str | None = None, base_path: str = ""
) -> str:
    url = f"{base_path}/{route}/{quote_path(zim, safe='')}"
    if path:
        url += f"/{quote_path(path, safe='/')}"
    return url


def _content_url(zim: str, path: str | None = None, base_path: str = "") -> str:
    return _route_url("content", zim, path, base_path)


def _article_url(zim: str, path: str | None = None, base_path: str = "") -> str:
    return _route_url("article", zim, path, base_path)


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
    return HTTPException(status_code=status, detail=str(error))


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
) -> str:
    zim_url = _content_url(zim, base_path=base_path)
    random_url = f"{base_path}/random?zim={quote_path(zim, safe='')}"
    zim_name = html_escape(str(zim), quote=True)
    site_name = _zim_site_name(zim)
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
            f'<a class="zimantic-toolbar-article" data-zimantic-permalink '
            f'data-zimantic-action="copy" href="{permalink_url}" '
            f'title="Open or copy this article\'s permalink">'
            f'<span class="zimantic-toolbar-article-full">{article_title}</span>'
            '<span class="zimantic-toolbar-article-short">'
            "Permalink</span></a>"
        )
        random_html = (
            f'<a href="{random_url}" data-zimantic-action="random" '
            f'data-zimantic-random-zim-url="{html_escape(base_path + "/random", quote=True)}" '
            f'title="Open a random article (Alt-click for a random ZIM)">Random</a>'
        )

    home_html = ""
    if error is None or error.get("zim_found"):
        home_html = (
            f'<a class="zimantic-toolbar-home" data-zimantic-home href="{zim_url}" '
            f'title="Open this ZIM\'s homepage ({zim_name})">'
            f'<span class="zimantic-toolbar-home-full">{html_escape(site_name)}</span>'
            '<span class="zimantic-toolbar-home-short">Home</span>'
            "</a>"
        )
    return f"""<nav data-zimantic-toolbar aria-label="Article navigation">
  <div class="zimantic-toolbar-nav">
    <button type="button" data-zimantic-action="close" title="Close article">Close</button>
    <span class="zimantic-toolbar-context-nav" role="group" aria-label="Adjacent search results">
      <button type="button" data-zimantic-action="previous"
        data-zimantic-context-previous hidden disabled aria-label="Previous search result"
        title="Open the previous search result">←</button>
      <button type="button" data-zimantic-action="next"
        data-zimantic-context-next hidden disabled aria-label="Next search result"
        title="Open the next search result">→</button>
    </span>
    {random_html}
    {home_html}
    <span class="zimantic-toolbar-breadcrumbs">{breadcrumb_html}</span>
  </div>
  <div class="zimantic-toolbar-actions">
    <span class="zimantic-toolbar-parent-nav-group" role="group" aria-label="Adjacent links in parent page">
      <button type="button" class="zimantic-toolbar-parent-nav"
        data-zimantic-action="parent-previous" data-zimantic-parent-previous
        hidden disabled aria-label="Previous link in parent page"
        title="Open the previous link in the parent page">↑</button>
      <button type="button" class="zimantic-toolbar-parent-nav"
        data-zimantic-action="parent-next" data-zimantic-parent-next
        hidden disabled aria-label="Next link in parent page"
        title="Open the next link in the parent page">↓</button>
    </span>
    <a class="zimantic-toolbar-search" href="{base_path}/" data-zimantic-action="home" title="Search your library">Zimantic</a>
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


def _decorate_article_bridge(
    content: bytes | memoryview, zim: str, base_path: str = "", xhtml: bool = False
) -> bytes | memoryview:
    """Add the link-reporting bridge and mobile fallback to a raw article.

    The article keeps its own markup; a small script reports link clicks to the
    /content/ shell, and desktop-only captures also get the mobile fallback
    described above. Non-HTML entries and documents that already carry the
    bridge are returned untouched.
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
    early = ARTICLE_INDEXEDDB_LOADER % json.dumps(
        f"{base_path}/indexeddb-shim.js"
    ) + ARTICLE_STORAGE_SHIM
    additions = (
        ARTICLE_COOKIE_SHIM
        + mobile_head
        + (ARTICLE_NAV_BRIDGE_SCRIPT % json.dumps(base_path))
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


def _content_shell(
    zim: str,
    article: dict,
    status_code: int = 200,
    fallback_icon: str | None = None,
    base_path: str = "",
    highlight: str | None = None,
    path_exists=None,
    error: dict | None = None,
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
    toolbar = _article_toolbar(
        zim,
        article,
        base_path,
        highlight,
        path_exists,
        error=error,
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
main[data-zimantic-shell-main] {
  display: flex;
  flex: 1 1 auto;
  flex-direction: column;
  min-height: 0;
  min-width: 0;
}
.zimantic-shell-heading {
  border: 0;
  clip: rect(0 0 0 0);
  height: 1px;
  margin: -1px;
  overflow: hidden;
  padding: 0;
  position: absolute;
  white-space: nowrap;
  width: 1px;
}
nav[data-zimantic-toolbar] {
  flex: 0 0 auto !important;
  margin: 0 !important;
  position: static !important;
}
[data-zimantic-storage-notice] {
  align-items: center;
  background: #fff8e1;
  border-bottom: 1px solid #ead9a1;
  color: #4a3b00;
  display: flex;
  flex: 0 0 auto;
  flex-wrap: wrap;
  font: 13px/1.45 system-ui, sans-serif;
  gap: 8px 12px;
  padding: 8px 12px;
}
[data-zimantic-storage-notice][hidden] { display: none; }
[data-zimantic-storage-notice] p {
  flex: 1 1 260px;
  margin: 0;
}
[data-zimantic-storage-notice] a,
[data-zimantic-storage-notice] button {
  background: #fff;
  border: 1px solid #cbb877;
  border-radius: 4px;
  color: inherit;
  cursor: pointer;
  flex: 0 0 auto;
  font: inherit;
  padding: 3px 10px;
  text-decoration: none;
}
[data-zimantic-storage-notice] a:hover,
[data-zimantic-storage-notice] button:hover { background: #f6ecc8; }
[data-zimantic-capture-notice] {
  align-items: center;
  background: #fff8e1;
  border-bottom: 1px solid #ead9a1;
  color: #4a3b00;
  display: flex;
  flex: 0 0 auto;
  flex-wrap: wrap;
  font: 13px/1.45 system-ui, sans-serif;
  gap: 8px 12px;
  padding: 8px 12px;
}
[data-zimantic-capture-notice][hidden] { display: none; }
[data-zimantic-capture-notice] p {
  flex: 1 1 260px;
  margin: 0;
}
[data-zimantic-capture-notice] button {
  background: #fff;
  border: 1px solid #cbb877;
  border-radius: 4px;
  color: inherit;
  cursor: pointer;
  flex: 0 0 auto;
  font: inherit;
  padding: 3px 10px;
}
[data-zimantic-capture-notice] button:hover { background: #f6ecc8; }
iframe[data-zimantic-article] {
  background: #fff;
  border: 0;
  display: block;
  flex: 1 1 auto !important;
  min-height: 0 !important;
  min-width: 0 !important;
  width: 100% !important;
}
.zimantic-shell-error {
  align-items: flex-start;
  display: flex;
  flex: 1 1 auto;
  flex-direction: column;
  gap: 10px;
  justify-content: center;
  margin: 0 auto;
  max-width: 720px;
  overflow: auto;
  padding: 40px 24px;
  width: 100%;
}
.zimantic-shell-error h1 {
  font: 700 1.5rem/1.25 system-ui, sans-serif;
  margin: 0;
}
.zimantic-shell-error p {
  color: #333;
  font: 15px/1.5 system-ui, sans-serif;
  margin: 0;
}
.zimantic-shell-error .zimantic-shell-error-path {
  color: #5d6673;
  font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
  font-size: 13px;
  overflow-wrap: anywhere;
}
.zimantic-shell-error .zimantic-shell-error-links a {
  color: #1f6fd6;
}
</style>
</head>
<body>
"""
        + toolbar
        + '<main data-zimantic-shell-main>'
        + (
            _content_error_panel(error, base_path)
            if error
            else (
                f'<h1 class="zimantic-shell-heading">{title}</h1>\n'
                "\n<div id=\"articleStorageNotice\" class=\"article-storage-notice\" "
                "data-zimantic-storage-notice role=\"status\" hidden>\n"
                "<p>This article uses browser storage, which is isolated inside this "
                "viewer. Changes may not be saved.</p>\n"
                "<a id=\"articleStorageNoticeOpen\" class=\"secondary\" "
                "data-zimantic-storage-notice-open href=\"#\" target=\"_blank\" "
                "rel=\"noopener noreferrer\">Open in a new tab "
                "<span aria-hidden=\"true\">↗</span></a>\n"
                "<button id=\"articleStorageNoticeDismiss\" class=\"secondary\" "
                "data-zimantic-storage-notice-dismiss type=\"button\">Dismiss</button>\n"
                "</div>\n"
                + "\n<div data-zimantic-capture-notice hidden>"
                "<p>This capture has no readable content. The archived page may have "
                "been empty (for example, a parked domain), or its scripts and styles "
                "may not have been saved.</p>"
                '<button type="button" data-zimantic-capture-notice-dismiss>Dismiss</button>'
                "</div>\n"
                + f'<iframe data-zimantic-article title="{title}" '
                f'src="{iframe_url}" referrerpolicy="no-referrer"></iframe>\n'
            )
        )
        + "</main>"
        + (ARTICLE_TOOLBAR_SCRIPT % json.dumps(base_path))
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


def create_app(searchClass, debug: bool = False) -> FastAPI:
    app = FastAPI(title="Zimantic")
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
        shutdown = getattr(searchClass, "close", None)
        if callable(shutdown):
            try:
                shutdown()
            except Exception as error:  # never mask the original shutdown path
                logger.warning("zimantic: search shutdown failed (%s)", error)
        try:
            PID_FILE.unlink()
        except OSError:
            pass

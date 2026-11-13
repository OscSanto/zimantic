(() => {
  const prefix = __BASE_PATH_JSON__;
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
  // Article captures often reference images that were not saved with the ZIM
  // (Stack Exchange profile avatars are the common case). Hide the browser's
  // broken-image glyph without changing the article layout.
  const markBrokenImage = image => {
    if (!image || image.tagName !== "IMG") return;
    if (!(image.currentSrc || image.getAttribute("src"))) return;
    if (image.complete && image.naturalWidth === 0) {
      image.setAttribute("data-zimantic-broken", "true");
    }
  };
  const scanBrokenImages = () => {
    if (typeof document.querySelectorAll !== "function") return;
    for (const image of document.querySelectorAll("img")) markBrokenImage(image);
  };
  document.addEventListener("error", event => {
    const image = event.target;
    if (image && image.tagName === "IMG") {
      image.setAttribute("data-zimantic-broken", "true");
    }
  }, true);
  document.addEventListener("load", event => {
    const image = event.target;
    if (image && image.tagName === "IMG" && image.naturalWidth > 0) {
      image.removeAttribute("data-zimantic-broken");
    }
  }, true);
  if (
    typeof document.createElement === "function" &&
    (document.head || document.documentElement) &&
    (!document.querySelector ||
      !document.querySelector("style[data-zimantic-broken-images]"))
  ) {
    const brokenImageStyle = document.createElement("style");
    brokenImageStyle.setAttribute("data-zimantic-broken-images", "true");
    brokenImageStyle.textContent =
      "img[data-zimantic-broken] { visibility: hidden !important; }";
    (document.head || document.documentElement).appendChild(brokenImageStyle);
  }
  document.addEventListener("DOMContentLoaded", scanBrokenImages, { once: true });
  const scanAddedImages = nodes => {
    for (const node of nodes || []) {
      if (!node || node.nodeType !== 1) continue;
      if (node.tagName === "IMG") {
        markBrokenImage(node);
      } else if (typeof node.querySelectorAll === "function") {
        for (const image of node.querySelectorAll("img")) {
          markBrokenImage(image);
        }
      }
    }
  };
  if (typeof MutationObserver === "function" && document.documentElement) {
    // Scan only inserted subtrees. Re-scanning every image after each DOM
    // mutation made script-driven captures schedule a full-document sweep
    // every 100 ms.
    new MutationObserver(mutations => {
      for (const mutation of mutations) {
        scanAddedImages(mutation.addedNodes);
      }
    }).observe(document.documentElement, {
      childList: true,
      subtree: true,
    });
  }
  const initialize = () => {
    const normalizeText = value =>
      String(value || "").replace(/\s+/g, " ").trim();
    const escapeRegExp = value =>
      String(value).replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
    const phrasePattern = words =>
      new RegExp(words.map(escapeRegExp).join("\\s+"), "i");
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
    if (window.parent !== window) {
      // A framed article can be cross-origin when secure browsing sandboxes
      // it, so the shell may not be able to inspect it directly. Report whether
      // there is anything to show and let the shell decide whether to display
      // its empty-capture notice.
      const reportVisibleContent = () => {
        const body = document.body;
        if (!body) return;
        let hasContent = Boolean(String(body.innerText || "").trim());
        if (!hasContent) {
          const media = body.querySelectorAll(
            "img, svg, canvas, video, audio, iframe, object, embed, " +
            "input, textarea, select"
          );
          for (const node of media) {
            const rect = node.getBoundingClientRect();
            if (rect.width < 8 || rect.height < 8) continue;
            if (node.tagName === "IMG" && !node.naturalWidth) continue;
            hasContent = true;
            break;
          }
        }
        window.parent.postMessage({
          type: "zimantic-article-content",
          hasContent,
        }, "*");
      };
      reportVisibleContent();
      setTimeout(reportVisibleContent, 700);
      setTimeout(reportVisibleContent, 1600);
      setTimeout(reportVisibleContent, 3000);
    }
  };
  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", initialize, {once: true});
  } else {
    initialize();
  }
})();

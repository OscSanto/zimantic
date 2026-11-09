(() => {
  const appBasePath = __APP_BASE_PATH_JSON__;
  const secureBrowsing = __SECURE_BROWSING_JSON__;
  const storageNotice = document.querySelector("[data-zimantic-storage-notice]");
  const storageNoticeOpen = document.querySelector(
    "[data-zimantic-storage-notice-open]"
  );
  const storageNoticeDismiss = document.querySelector(
    "[data-zimantic-storage-notice-dismiss]"
  );
  let storageNoticeDismissed = false;
  // Secure browsing sandboxes the article document, so a standalone /content/
  // tab is isolated too. Do not offer it as a persistent-storage escape hatch.
  if (storageNoticeOpen) storageNoticeOpen.hidden = secureBrowsing;
  const showStorageNotice = () => {
    if (storageNoticeDismissed || !storageNotice) return;
    if (storageNoticeOpen && !secureBrowsing) storageNoticeOpen.href = location.href;
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
    // Ask the host to open the standalone view; an embedded shell may not be
    // allowed to open a top-level tab itself.
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
  // navigate the shell frame instead of the top-level app. Let the host close
  // the modal and return to search instead.
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
    const zimSearchForm = toolbar.querySelector("[data-zimantic-zim-search]");
    const zimSearchInput = toolbar.querySelector("[data-zimantic-zim-search-input]");
    const zimSuggestions = toolbar.querySelector("[data-zimantic-zim-suggestions]");
    const articleFrame = document.querySelector("iframe[data-zimantic-article]");
    const shellUrl = new URL(location.href);
    const localPath = pathname => {
      if (!appBasePath) return pathname;
      if (pathname === appBasePath) return "/";
      return pathname.startsWith(`${appBasePath}/`)
        ? pathname.slice(appBasePath.length)
        : null;
    };
    // The iframe src is the authoritative raw-article URL; deriving it from
    // the shell path would repeat the server's route mapping and break if the
    // shell were ever mounted somewhere other than /content.
    const articleUrl = articleFrame
      ? new URL(articleFrame.getAttribute("src") || shellUrl.href, shellUrl.href)
      : null;
    const permalinkLabel = permalink ? permalink.innerHTML : "";
    let permalinkResetTimer = null;
    let selectedSearchText = "";
    let articleSelection = "";
    let offlineRequest = 0;
    const normalizeSelection = value =>
      String(value || "").replace(/\s+/g, " ").trim().slice(0, 2048);
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
      // Compare parsed URLs rather than relying on location.origin, which is
      // null for any shell that is served in a secure-mode article sandbox.
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
      String(value || "").toLowerCase().replace(/\.$/, "").replace(/^www\./, "");
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
        `${encodeURIComponent(root)}/${url.pathname.replace(/^\/+/, "")}`;
      local.search = url.search;
      local.hash = url.hash;
      return local.href;
    };
    const currentContentUrl = route => {
      const path = contentPathFor(route.url);
      return path ? path + route.url.search : null;
    };
    const handleArticleLink = data => {
      const route = articleRoute(data?.url) ||
        (articleUrl ? articleRoute(articleUrl.href) : null);
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
      // Navigate the top-level shell directly; inside the modal, ask the host
      // to navigate so the toolbar and modal state stay in sync.
      if (url.origin === shellUrl.origin) {
        if (window.parent !== window) {
          window.parent.postMessage({
            type: "zimantic-app-navigate",
            url: url.href,
          }, "*");
          return;
        }
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
      if (event.data?.type === "zimantic-article-content") {
        articleContentReport = Boolean(event.data.hasContent);
        if (articleContentReport) {
          hideCaptureNotice();
        } else if (!captureNoticeDismissed) {
          scheduleCaptureCheck();
        }
        return;
      }
      if (event.data?.type !== "zimantic-article-link") return;
      handleArticleLink(event.data);
    });
    // Search inside the ZIM currently open in this shell. Suggestions are
    // title-only, so a 500 ms typing pause is enough for the server to answer
    // without starting a full hybrid search on every keystroke.
    const zimSearchDelay = 500;
    const zimSearchLimit = 8;
    let zimSearchTimer = null;
    let zimSearchController = null;
    let zimSearchSequence = 0;
    let zimSearchItems = [];
    let zimSearchActive = -1;
    let zimSearchComposing = false;
    const resetZimSuggestionSelection = () => {
      zimSearchActive = -1;
      if (zimSearchInput) zimSearchInput.removeAttribute("aria-activedescendant");
      if (!zimSuggestions) return;
      zimSuggestions.querySelectorAll("[role='option']").forEach(option => {
        option.setAttribute("aria-selected", "false");
      });
    };
    const closeZimSuggestions = () => {
      zimSearchItems = [];
      resetZimSuggestionSelection();
      if (zimSuggestions) {
        zimSuggestions.hidden = true;
        zimSuggestions.replaceChildren();
      }
      if (zimSearchInput) {
        zimSearchInput.setAttribute("aria-expanded", "false");
      }
    };
    const setActiveZimSuggestion = index => {
      if (!zimSuggestions || !zimSearchItems.length) return;
      zimSearchActive = Math.max(0, Math.min(index, zimSearchItems.length - 1));
      const options = zimSuggestions.querySelectorAll("[role='option']");
      options.forEach((option, optionIndex) => {
        const selected = optionIndex === zimSearchActive;
        option.setAttribute("aria-selected", selected ? "true" : "false");
        if (!selected) return;
        zimSearchInput?.setAttribute("aria-activedescendant", option.id);
        option.scrollIntoView?.({block: "nearest"});
      });
    };
    const renderZimSuggestions = items => {
      if (!zimSuggestions) return;
      zimSearchItems = items;
      zimSuggestions.replaceChildren();
      resetZimSuggestionSelection();
      if (!items.length) {
        closeZimSuggestions();
        return;
      }
      items.forEach((item, index) => {
        const option = document.createElement("div");
        option.className = "zimantic-toolbar-suggestion";
        option.id = `zimantic-zim-suggestion-${index}`;
        option.setAttribute("role", "option");
        option.setAttribute("aria-selected", "false");
        const title = document.createElement("span");
        title.className = "zimantic-toolbar-suggestion-title";
        title.textContent = String(item.title || item.path || "(untitled)");
        const path = document.createElement("span");
        path.className = "zimantic-toolbar-suggestion-path";
        path.textContent = String(item.path || "");
        option.append(title);
        if (path.textContent) option.append(path);
        option.addEventListener("pointerdown", event => {
          event.preventDefault();
          openZimSuggestion(index);
        });
        zimSuggestions.append(option);
      });
      zimSuggestions.hidden = false;
      zimSearchInput?.setAttribute("aria-expanded", "true");
    };
    const openZimSuggestion = index => {
      const item = zimSearchItems[index];
      if (!item) return;
      const route = articleUrl ? articleRoute(articleUrl.href) : null;
      let target;
      try {
        if (item.url) {
          target = new URL(String(item.url), location.href);
        } else {
          const zim = String(zimSearchForm?.dataset.zimanticZim || "");
          const path = String(item.path || "")
            .split("/")
            .filter(Boolean)
            .map(part => `/${encodeURIComponent(part)}`)
            .join("");
          target = new URL(
            `${appBasePath}/content/${encodeURIComponent(zim)}${path}`,
            location.href,
          );
        }
      } catch (_) {
        return;
      }
      if (target.origin !== shellUrl.origin) return;
      if (zimSearchInput) zimSearchInput.value = String(item.title || "");
      closeZimSuggestions();
      if (window.parent !== window) {
        window.parent.postMessage({
          type: "zimantic-article-follow",
          url: target.href,
          parent: route ? currentContentUrl(route) : null,
        }, "*");
        return;
      }
      offlineRequest += 1;
      location.href = target.href;
    };
    const openZimSearchPage = () => {
      if (!zimSearchInput || !zimSearchForm) return false;
      const query = zimSearchInput.value.trim();
      const zim = String(zimSearchForm.dataset.zimanticZim || "").trim();
      if (!query || !zim) return false;
      const target = new URL(`${appBasePath}/`, location.href);
      target.searchParams.set("q", query);
      target.searchParams.set("source", zim);
      closeZimSuggestions();
      if (window.parent !== window) {
        window.parent.postMessage({
          type: "zimantic-app-navigate",
          url: target.href,
        }, "*");
      } else {
        location.href = target.href;
      }
      return true;
    };
    const requestZimSuggestions = query => {
      if (!zimSearchForm) return;
      const zim = String(zimSearchForm.dataset.zimanticZim || "").trim();
      if (!zim) return;
      if (zimSearchController) zimSearchController.abort();
      const controller = new AbortController();
      zimSearchController = controller;
      const sequence = ++zimSearchSequence;
      const params = new URLSearchParams({
        q: query,
        source: zim,
        limit: String(zimSearchLimit),
      });
      fetch(`${appBasePath}/suggest?${params}`, {
        signal: controller.signal,
        headers: {Accept: "application/json"},
      })
        .then(response => {
          if (!response.ok) throw new Error(`HTTP ${response.status}`);
          return response.json();
        })
        .then(items => {
          if (sequence !== zimSearchSequence) return;
          renderZimSuggestions(Array.isArray(items) ? items : []);
        })
        .catch(error => {
          if (error.name === "AbortError") return;
          if (sequence === zimSearchSequence) closeZimSuggestions();
        })
        .finally(() => {
          if (zimSearchController === controller) zimSearchController = null;
        });
    };
    const scheduleZimSuggestions = () => {
      if (!zimSearchInput) return;
      clearTimeout(zimSearchTimer);
      zimSearchTimer = null;
      // A new keystroke invalidates in-flight responses and any keyboard
      // selection. Keep the list already on screen visible and clickable until
      // the next response replaces it.
      zimSearchSequence += 1;
      if (zimSearchController) {
        zimSearchController.abort();
        zimSearchController = null;
      }
      resetZimSuggestionSelection();
      const query = zimSearchInput.value.trim();
      if (!query) {
        closeZimSuggestions();
        return;
      }
      zimSearchTimer = setTimeout(() => {
        zimSearchTimer = null;
        requestZimSuggestions(query);
      }, zimSearchDelay);
    };
    const submitZimSearch = () => {
      if (zimSearchActive >= 0) {
        openZimSuggestion(zimSearchActive);
        return true;
      }
      return openZimSearchPage();
    };
    if (zimSearchForm && zimSearchInput) {
      zimSearchInput.addEventListener("compositionstart", () => {
        zimSearchComposing = true;
      });
      zimSearchInput.addEventListener("compositionend", () => {
        zimSearchComposing = false;
        scheduleZimSuggestions();
      });
      zimSearchInput.addEventListener("input", () => {
        if (!zimSearchComposing) scheduleZimSuggestions();
      });
      zimSearchInput.addEventListener("keydown", event => {
        if (event.key === "ArrowDown" || event.key === "ArrowUp") {
          if (!zimSearchItems.length) return;
          event.preventDefault();
          const offset = event.key === "ArrowDown" ? 1 : -1;
          const start = zimSearchActive < 0
            ? (offset > 0 ? -1 : 0)
            : zimSearchActive;
          setActiveZimSuggestion(start + offset);
          return;
        }
        if (
          event.key === "Tab" &&
          !event.shiftKey &&
          !event.ctrlKey &&
          !event.metaKey &&
          !event.altKey
        ) {
          if (!zimSearchItems.length || zimSearchActive >= 0) return;
          event.preventDefault();
          setActiveZimSuggestion(0);
          return;
        }
        if (event.key === "Escape") {
          if (!zimSearchItems.length) return;
          event.preventDefault();
          event.stopPropagation();
          closeZimSuggestions();
          return;
        }
        if (event.key === "Enter") {
          event.preventDefault();
          submitZimSearch();
        }
      });
      zimSearchForm.addEventListener("submit", event => {
        event.preventDefault();
        submitZimSearch();
      });
      zimSearchForm.addEventListener("focusout", event => {
        if (!zimSearchForm.contains(event.relatedTarget)) closeZimSuggestions();
      });
    }
    // A capture can finish loading with nothing to show: a JavaScript shell that
    // renders content it never saved, a redirect to a page whose assets 404, or
    // a parked domain. An empty iframe looks broken, so explain it in the shell.
    const captureNotice = document.querySelector("[data-zimantic-capture-notice]");
    const captureNoticeDismiss = document.querySelector(
      "[data-zimantic-capture-notice-dismiss]"
    );
    let captureNoticeTimer = null;
    let captureNoticeDismissed = false;
    let articleContentReport = null;
    const captureHasVisibleContent = () => {
      if (articleContentReport !== null) return articleContentReport;
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
      articleFrame.addEventListener("load", () => {
        articleContentReport = null;
        scheduleCaptureCheck();
      });
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
      return (selection?.toString() || "").replace(/\s+/g, " ").trim().slice(0, 4096);
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
      } else if (action === "source-previous" || action === "source-next") {
        // ZIM switching is independent of search-result and parent navigation.
        const url = control.dataset.zimanticUrl;
        if (!url) return;
        event.preventDefault();
        if (window.parent !== window) {
          const route = articleUrl ? articleRoute(articleUrl.href) : null;
          window.parent.postMessage({
            type: "zimantic-article-follow",
            url,
            parent: route ? currentContentUrl(route) : null,
          }, "*");
          return;
        }
        offlineRequest += 1;
        location.href = url;
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
          // Let the top-level page perform the write; this also works when
          // secure browsing's sandbox blocks clipboard access.
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

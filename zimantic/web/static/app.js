        (function () {
            "use strict";

            const $ = (id) => document.getElementById(id);
            const BASE_PATH = (() => {
                const script = document.currentScript;
                if (!script) return "";
                const path = new URL(script.src, document.baseURI).pathname;
                return path.endsWith("/app.js") ? path.slice(0, -"/app.js".length) : "";
            })();
            const appPath = (path) => `${BASE_PATH}${path}`;
            const stripBasePath = (pathname) => {
                if (!BASE_PATH) return pathname;
                if (pathname === BASE_PATH) return "/";
                return pathname.startsWith(`${BASE_PATH}/`)
                    ? pathname.slice(BASE_PATH.length)
                    : pathname;
            };
            let PAGE_SIZE = 10; // replaced with the server's page_size at startup
            let debugMode = false;
            // Filled from /config so highlighting uses the same stopword list as
            // the server instead of maintaining a second copy here.
            let HIGHLIGHT_STOPWORDS = new Set();
            const AUTO_SUBMIT_DELAY = 800;
            const AUTO_HISTORY_DELAY = 3000;
            let MAX_QUERY_LENGTH = 4096;
            // Streaming renders at two cadences:
            //  - Fast: the status line ("n/m sources complete") is updated
            //    immediately on every event, straight from the event handler.
            //  - Slow: everything else (result list, source filters, pager,
            //    notice) is re-rendered on this single throttled cadence.
            // Source/snapshot events coalesce into one render per interval so a
            // burst of completions can't thrash the DOM.
            const STREAM_RENDER_DELAY = 700;
            const STREAM_SEARCH_TIMEOUT = 15000;
            const BUSY_RETRY_BASE_DELAY = 1000;
            const BUSY_RETRY_MAX_DELAY = 8000;
            const BUSY_RETRY_LIMIT = 5;
            const THUMB_LIMIT = 128 * 1024;
            const THUMB_CONCURRENCY = 4;
            let SOURCE_FILTER_LIMIT = 10;
            const SOURCE_FILTER_QUERY_LIMIT = 30;
            let sourceFilterShowAll = false;
            let sourceFilterShowOther = false;
            const SOURCE_CACHE_KEY = "zimantic.sources.v2";
            const LIBRARY_CACHE_KEY = "zimantic.library.v5";
            const reducedMotion = () => matchMedia("(prefers-reduced-motion: reduce)").matches;
            let sources = [];
            let librarySources = [];
            let current = null;
            let previousHeadingSearch = null;
            let selectedSourceKeys = new Set();
            let requestController = null;
            let pageController = null;
            let searchSequence = 0;
            let autoSubmitTimer = null;
            let autoSearchBase = null;
            let autoSearchCommitTimer = null;
            let composing = false;
            let resultList = null;
            let thumbnailGeneration = 0;
            const thumbnailCache = new Map();
            const thumbnailQueue = [];
            const thumbnailControllers = new Set();
            let activeThumbnails = 0;
            let streamRenderTimer = null;
            let lastStreamRenderAt = 0;
            let lastRenderedItems = null;
            let modalPreviousFocus = null;
            let libraryInfoPreviousFocus = null;
            let libraryInfoCurrentKey = null;
            let libraryInfoHistoryPushed = false;
            let activeModalUrl = null;
            let activeSearchResult = null;
            let activeLibraryArticleUrl = null;
            let previousModalArticleUrl = null;
            let pendingModalArticleUrl = null;
            let pendingModalNavigation = null;
            let offlineLinkRequest = 0;
            let activeParentNavigation = null;
            let activeParentUrl = null;
            let parentNavigationSequence = 0;
            const parentNavigationCache = new Map();
            const articleAliases = new Map();
            const defaultDocumentTitle = document.title;
            const defaultIconLinks = [
                ...document.querySelectorAll("link[rel~='icon'], link[rel='apple-touch-icon']"),
            ];
            let articleIconLink = null;
            let articleIconsHidden = false;

            function cancelStreamRender() {
                if (streamRenderTimer !== null) {
                    clearTimeout(streamRenderTimer);
                    streamRenderTimer = null;
                }
            }

            // Coalesce source/snapshot events into one full-view render per
            // STREAM_RENDER_DELAY. The status line is deliberately not handled
            // here — it is the fast path, updated directly as each event arrives.
            //
            // The wait is measured from the previous render, not from the latest
            // event. Measuring from the event lets a snapshot that lands just
            // after a render push the next one a full interval out, stretching
            // the visible cadence to ~2x. Measuring from the last render keeps
            // the cadence steady at STREAM_RENDER_DELAY while still painting the
            // latest state within one interval of the last event.
            function scheduleStreamRender(sequence) {
                if (streamRenderTimer !== null) return;
                const now = performance.now();
                const wait = Math.max(0, STREAM_RENDER_DELAY - (now - lastStreamRenderAt));
                streamRenderTimer = setTimeout(() => {
                    streamRenderTimer = null;
                    lastStreamRenderAt = performance.now();
                    if (sequence === searchSequence && current) {
                        renderView();
                        renderNotice();
                    }
                }, wait);
            }

            function yieldToBrowser() {
                return new Promise((resolve) => {
                    if (typeof requestAnimationFrame === "function") {
                        requestAnimationFrame(resolve);
                    } else {
                        setTimeout(resolve, 0);
                    }
                });
            }

            function escapeHtml(value) {
                return String(value).replace(
                    /[&<>"']/g,
                    (c) =>
                        ({
                            "&": "&amp;",
                            "<": "&lt;",
                            ">": "&gt;",
                            '"': "&quot;",
                            "'": "&#39;",
                        })[c],
                );
            }

            function contentUrl(zim, path = null) {
                const encodedZim = encodeURIComponent(String(zim || ""));
                if (!path) return appPath(`/content/${encodedZim}`);
                const encodedPath = String(path)
                    .split("/")
                    .filter(Boolean)
                    .map((part) => encodeURIComponent(part))
                    .join("/");
                return appPath(encodedPath ? `/content/${encodedZim}/${encodedPath}` : `/content/${encodedZim}`);
            }

            function formatLibrarySize(bytes) {
                const value = Number(bytes);
                if (!Number.isFinite(value) || value <= 0) return "";
                const units = ["B", "KB", "MB", "GB", "TB"];
                let size = value;
                let unit = 0;
                while (size >= 1024 && unit < units.length - 1) {
                    size /= 1024;
                    unit++;
                }
                return `${size >= 10 || unit === 0 ? Math.round(size) : size.toFixed(1)} ${units[unit]}`;
            }

            function libraryTitle(source) {
                return String(source.title || source.name || source.book || "Untitled source").trim();
            }

            function sourceRecord(keyOrSource) {
                if (keyOrSource && typeof keyOrSource === "object") return keyOrSource;
                const wanted = String(keyOrSource || "").trim();
                if (!wanted) return null;
                const matches = (source) =>
                    [source.key, source.local_name, source.book, source.name].some(
                        (value) => String(value || "") === wanted,
                    );
                return sources.find(matches) || librarySources.find(matches) || null;
            }

            function sourceDisplayName(keyOrSource) {
                const source = sourceRecord(keyOrSource);
                if (source) return libraryTitle(source);
                const fallback = typeof keyOrSource === "string" ? keyOrSource : "";
                return fallback || "Unknown source";
            }

            function sourceStemName(keyOrSource) {
                const source = sourceRecord(keyOrSource);
                if (!source) {
                    return typeof keyOrSource === "string" ? keyOrSource : "";
                }
                return String(
                    source.local_name || source.book || source.name || source.key || "",
                ).trim();
            }

            function librarySearchText(source) {
                const tags = Array.isArray(source.tags) ? source.tags : [];
                return [
                    source.title,
                    source.name,
                    source.local_name,
                    source.book,
                    source.key,
                    source.description,
                    source.long_description,
                    source.creator,
                    source.publisher,
                    source.language,
                    source.date,
                    source.category,
                    source.flavour,
                    source.license,
                    source.scraper,
                    source.source,
                    source.attribution,
                    source.relation,
                    source.perma_ref,
                    source.uuid,
                    source.homepage,
                    source.has_fulltext_index,
                    source.searchable,
                    source.icon_url,
                    source.mode,
                    source.error,
                    source.counter,
                    source.article_count,
                    source.media_count,
                    formatLibrarySize(source.size),
                    ...tags,
                ]
                    .filter(Boolean)
                    .join(" ")
                    .toLocaleLowerCase();
            }

            function libraryHomepageUrl(source) {
                const zim = source.local_name || source.book || source.name;
                if (!zim) return "";
                if (source.homepage) return contentUrl(zim, source.homepage);
                // A readable archive can lack a resolvable main entry. If it is
                // searchable, open a random article instead of a broken
                // homepage link; otherwise the card has Details only.
                if (source.searchable !== false) {
                    return appPath(`/random?zim=${encodeURIComponent(zim)}`);
                }
                return "";
            }

            function libraryLanguage(source, join = false) {
                const parts = String(source.language || "")
                    .split(",")
                    .map((part) => part.trim())
                    .filter(Boolean);
                if (!parts.length) return "";
                if (join) return parts.join(", ");
                return parts.length > 1 ? "MUL" : parts[0].toUpperCase();
            }

            function infoUrl(value) {
                const text = String(value || "").trim();
                if (!text) return "";
                if (/^https?:\/\//i.test(text)) return text;
                if (/^[a-z0-9.-]+\.[a-z]{2,}(\/.*)?$/i.test(text)) {
                    return `https://${text}`;
                }
                return "";
            }

            function infoValue(value) {
                return value === null || value === undefined ? "" : String(value).trim();
            }

            function appendInfoRow(list, label, value, options = {}) {
                const text = infoValue(value);
                if (!text) return;
                const term = document.createElement("dt");
                term.textContent = label;
                const detail = document.createElement("dd");
                const href = options.href === false ? "" : infoUrl(text);
                if (href) {
                    const link = document.createElement("a");
                    link.href = href;
                    link.target = "_blank";
                    link.rel = "noopener noreferrer";
                    link.textContent = text;
                    detail.appendChild(link);
                } else {
                    detail.textContent = text;
                }
                list.append(term, detail);
            }

            function counterSummary(counter) {
                return String(counter || "")
                    .split(";")
                    .map((entry) => {
                        const [mime, count] = entry.split("=");
                        return {
                            mime: String(mime || "").trim(),
                            count: Number(count),
                        };
                    })
                    .filter((entry) => entry.mime && Number.isFinite(entry.count) && entry.count > 0)
                    .sort((left, right) => right.count - left.count);
            }

            function searchWithinLibrarySource(source) {
                if (source.searchable === false) return;
                // The search page replaces this document, so do not try to pop
                // a history entry for the details modal on the way out.
                closeLibraryInfo({ navigateHistory: false });
                const key = source.key || source.local_name || source.book || source.name;
                if (!key) return;
                const target = new URL(makeStateUrl("", [key], 0), location.href);
                target.pathname = appPath("/");
                // The search view has no library filter.
                target.searchParams.delete("filter");
                window.location.assign(target.toString());
            }

            function libraryInfoSourceKey(source) {
                return String(
                    source.key || source.local_name || source.book || source.name || "",
                );
            }

            function closeLibraryInfo({ navigateHistory = true } = {}) {
                const modal = $("libraryInfoModal");
                if (!modal || modal.hidden) return;
                const state = history.state;
                const hadHistoryEntry = Boolean(state?.zimanticLibraryInfo);
                const pushedEntry = libraryInfoHistoryPushed;
                libraryInfoHistoryPushed = false;
                libraryInfoCurrentKey = null;
                modal.hidden = true;
                document.body.classList.remove("library-info-open");
                const wrap = document.querySelector(".wrap");
                if (wrap) wrap.inert = false;
                if (
                    libraryInfoPreviousFocus &&
                    typeof libraryInfoPreviousFocus.focus === "function"
                ) {
                    libraryInfoPreviousFocus.focus();
                }
                libraryInfoPreviousFocus = null;
                if (!navigateHistory || !hadHistoryEntry) return;
                if (pushedEntry) {
                    // Drop the entry we added when the details dialog opened;
                    // this returns to the same library URL without leaving the
                    // page, and the popstate handler ignores the hidden modal.
                    history.back();
                    return;
                }
                // Arrived here with Forward from an older entry: remove the
                // marker in place so closing the dialog does not add history.
                const next = { ...state };
                delete next.zimanticLibraryInfo;
                history.replaceState(
                    Object.keys(next).length ? next : null,
                    "",
                    location.href,
                );
            }

            function syncLibraryInfo(key) {
                if (!key || location.pathname !== appPath("/library")) {
                    closeLibraryInfo({ navigateHistory: false });
                    return;
                }
                const source = librarySources.find(
                    (item) => libraryInfoSourceKey(item) === String(key),
                );
                if (!source) {
                    closeLibraryInfo({ navigateHistory: false });
                    return;
                }
                if (!$("libraryInfoModal").hidden && libraryInfoCurrentKey === String(key)) {
                    return;
                }
                showLibraryInfo(source, { pushHistory: false });
            }

            function showLibraryInfo(source, { pushHistory = true } = {}) {
                const modal = $("libraryInfoModal");
                const content = $("libraryInfoContent");
                if (!modal || !content) return;
                libraryInfoCurrentKey = libraryInfoSourceKey(source);
                libraryInfoPreviousFocus = document.activeElement;
                const title = libraryTitle(source);
                const homepage = libraryHomepageUrl(source);
                const available = source.available !== false;
                content.replaceChildren();

                const header = document.createElement("div");
                header.className = "library-info-header";
                const thumb = document.createElement("div");
                thumb.className = "library-info-thumb";
                if (source.icon_url) {
                    const image = document.createElement("img");
                    image.src = source.icon_url;
                    image.alt = "";
                    image.addEventListener("error", () => {
                        image.remove();
                        thumb.textContent = title.slice(0, 1).toUpperCase();
                    }, { once: true });
                    thumb.appendChild(image);
                } else {
                    thumb.textContent = title.slice(0, 1).toUpperCase();
                }
                const heading = document.createElement("div");
                heading.className = "library-info-heading";
                const headingTitle = document.createElement("h2");
                headingTitle.id = "libraryInfoTitle";
                headingTitle.textContent = title;
                heading.appendChild(headingTitle);
                const subtitle = [
                    source.creator && `By ${source.creator}`,
                    source.date && String(source.date),
                    libraryLanguage(source, true),
                ].filter(Boolean).join(" · ");
                if (subtitle) {
                    const line = document.createElement("p");
                    line.className = "library-info-subtitle";
                    line.textContent = subtitle;
                    heading.appendChild(line);
                }
                header.append(thumb, heading);
                content.appendChild(header);

                const description = source.long_description || source.description;
                if (description) {
                    const text = document.createElement("p");
                    text.className = "library-info-description";
                    text.textContent = description;
                    content.appendChild(text);
                }

                const searchable = source.searchable !== false;
                if (available && (homepage || searchable)) {
                    const actions = document.createElement("div");
                    actions.className = "library-info-actions";
                    if (homepage) {
                        const open = document.createElement("a");
                        open.className = "library-button library-info-action";
                        open.href = homepage;
                        open.dataset.libraryArticle = "true";
                        open.textContent = source.homepage ? "Open" : "Random article";
                        actions.append(open);
                    }
                    if (searchable) {
                        const search = document.createElement("button");
                        search.type = "button";
                        search.className = "library-button library-info-action";
                        search.textContent = "Search";
                        search.addEventListener("click", () => searchWithinLibrarySource(source));
                        actions.append(search);
                    }
                    content.appendChild(actions);
                } else if (!available) {
                    const unavailable = document.createElement("p");
                    unavailable.className = "library-info-unavailable";
                    unavailable.textContent =
                        source.error || "This source is currently unavailable.";
                    content.appendChild(unavailable);
                } else {
                    const unavailable = document.createElement("p");
                    unavailable.className = "library-info-unavailable";
                    unavailable.textContent =
                        "This source has no homepage and cannot be searched yet.";
                    content.appendChild(unavailable);
                }

                const list = document.createElement("dl");
                list.className = "library-info-meta";
                appendInfoRow(list, "Publisher", source.publisher);
                appendInfoRow(list, "Creator", source.creator);
                appendInfoRow(list, "Language", libraryLanguage(source, true));
                appendInfoRow(list, "Date", source.date);
                appendInfoRow(list, "Size", formatLibrarySize(source.size));
                if (Number(source.article_count) > 0) {
                    appendInfoRow(list, "Articles", Number(source.article_count).toLocaleString());
                }
                if (Number(source.media_count) > 0) {
                    appendInfoRow(list, "Media", Number(source.media_count).toLocaleString());
                }
                appendInfoRow(list, "Category", source.category);
                appendInfoRow(list, "Flavour", source.flavour);
                appendInfoRow(
                    list,
                    "Tags",
                    Array.isArray(source.tags) ? source.tags.filter(Boolean).join(" · ") : "",
                );
                appendInfoRow(list, "License", source.license);
                appendInfoRow(list, "Scraper", source.scraper);
                appendInfoRow(list, "Source", source.source);
                appendInfoRow(list, "Attribution", source.attribution, { href: false });
                appendInfoRow(list, "Relation", source.relation);
                appendInfoRow(list, "Source name", source.perma_ref);
                if (list.children.length) content.appendChild(list);

                const counter = counterSummary(source.counter);
                if (counter.length) {
                    const details = document.createElement("details");
                    details.className = "library-info-counter";
                    const summary = document.createElement("summary");
                    summary.textContent = "Content types";
                    details.appendChild(summary);
                    const code = document.createElement("code");
                    code.textContent = counter
                        .map((entry) => `${entry.mime}: ${entry.count.toLocaleString()}`)
                        .join("\n");
                    details.appendChild(code);
                    content.appendChild(details);
                }

                modal.hidden = false;
                document.body.classList.add("library-info-open");
                const wrap = document.querySelector(".wrap");
                if (wrap) wrap.inert = true;
                $("libraryInfoClose")?.focus();
                if (pushHistory && libraryInfoCurrentKey) {
                    history.pushState(
                        {
                            ...(history.state || {}),
                            zimanticLibraryInfo: libraryInfoCurrentKey,
                        },
                        "",
                        location.href,
                    );
                    libraryInfoHistoryPushed = true;
                }
            }

            function shuffleLibrary() {
                for (let index = librarySources.length - 1; index > 0; index--) {
                    const other = Math.floor(Math.random() * (index + 1));
                    [librarySources[index], librarySources[other]] = [
                        librarySources[other],
                        librarySources[index],
                    ];
                }
                renderLibrary();
            }

            function readJsonCache(key) {
                try {
                    const value = JSON.parse(localStorage.getItem(key) || "null");
                    return Array.isArray(value) ? value : null;
                } catch (_) {
                    return null;
                }
            }

            function writeJsonCache(key, value) {
                try {
                    localStorage.setItem(key, JSON.stringify(value));
                } catch (_) {
                    // Storage is an optimization; private browsing and quota limits
                    // must not prevent the page from loading fresh data.
                }
            }

            async function fetchCachedJson(path, key) {
                const cached = readJsonCache(key);
                try {
                    const response = await fetch(path, { cache: "no-cache" });
                    if (!response.ok) throw new Error(`HTTP ${response.status}`);
                    const payload = await response.json();
                    if (!Array.isArray(payload)) throw new Error("invalid JSON payload");
                    writeJsonCache(key, payload);
                    return { payload, stale: false, error: null };
                } catch (error) {
                    if (cached) return { payload: cached, stale: true, error };
                    throw error;
                }
            }

            function readLibraryFilter() {
                return new URLSearchParams(location.search).get("filter") || "";
            }

            function updateLibraryFilterUrl() {
                if (location.pathname !== appPath("/library")) return;
                const filter = $("libraryFilter")?.value.trim() || "";
                const url = new URL(location.href);
                if (filter) {
                    if (url.searchParams.get("filter") === filter) return;
                    url.searchParams.set("filter", filter);
                } else {
                    if (!url.searchParams.has("filter")) return;
                    url.searchParams.delete("filter");
                }
                history.replaceState(
                    history.state,
                    "",
                    url.pathname + url.search + url.hash,
                );
            }

            function sortLibrarySources(sources) {
                return [...sources].sort((left, right) => {
                    const byTitle = libraryTitle(left).localeCompare(
                        libraryTitle(right),
                        undefined,
                        { sensitivity: "base", numeric: true },
                    );
                    if (byTitle) return byTitle;
                    return String(
                        left.key || left.local_name || left.name || "",
                    ).localeCompare(
                        String(right.key || right.local_name || right.name || ""),
                    );
                });
            }

            function renderLibrary() {
                const grid = $("libraryGrid");
                const status = $("libraryStatus");
                if (!grid || !status) return;
                const filter = $("libraryFilter");
                if (filter) {
                    const count = librarySources.length;
                    filter.placeholder = `Filter ${count.toLocaleString()} source${count === 1 ? "" : "s"}`;
                }
                grid.replaceChildren();
                if (!librarySources.length) {
                    status.textContent = "No installed sources found.";
                    const empty = document.createElement("p");
                    empty.className = "empty";
                    empty.textContent =
                        "Add a .zim file to zim_dir, then reload the server.";
                    grid.appendChild(empty);
                    return;
                }
                const query = $("libraryFilter").value.trim().toLocaleLowerCase();
                const terms = query.split(/\s+/).filter(Boolean);
                const excludedTerms = terms
                    .filter((term) => term.startsWith("-") && term.length > 1)
                    .map((term) => term.slice(1));
                const includedTerms = terms.filter(
                    (term) => !term.startsWith("-") || term.length === 1,
                );
                const visibleSources = query
                    ? librarySources.filter((source) => {
                          const searchable = librarySearchText(source);
                          return (
                              includedTerms.every((term) => searchable.includes(term)) &&
                              excludedTerms.every((term) => !searchable.includes(term))
                          );
                      })
                    : librarySources;
                status.textContent = query
                    ? `${visibleSources.length} of ${librarySources.length} installed sources`
                    : "";
                if (!visibleSources.length) {
                    const empty = document.createElement("p");
                    empty.className = "empty";
                    empty.textContent = "No installed source matches that filter.";
                    grid.appendChild(empty);
                    return;
                }
                visibleSources.forEach((source) => {
                    const title = libraryTitle(source);
                    const openUrl = libraryHomepageUrl(source);
                    const stem = sourceStemName(source);
                    const card = document.createElement("article");
                    card.className = "library-card";
                    const available = source.available !== false;
                    const linkable = available && Boolean(openUrl);

                    const preview = document.createElement(linkable ? "a" : "div");
                    preview.className = "library-card-preview";
                    if (stem) preview.title = stem;
                    if (linkable) {
                        preview.href = openUrl;
                        preview.dataset.libraryArticle = "true";
                        preview.setAttribute("aria-label", `Open ${title}`);
                    } else if (!available) {
                        // The card explains the problem next to an "Unavailable"
                        // badge, so dim the artwork instead of replacing it with
                        // text (an unreadable ZIM usually still has a favicon).
                        preview.classList.add("library-card-unavailable");
                        preview.setAttribute("aria-hidden", "true");
                    }
                    if (source.icon_url) {
                        const image = document.createElement("img");
                        image.src = source.icon_url;
                        image.alt = "";
                        image.loading = "lazy";
                        image.decoding = "async";
                        image.addEventListener("error", () => {
                            image.remove();
                            preview.classList.add("library-card-placeholder");
                            preview.textContent = title.slice(0, 1).toUpperCase();
                        }, { once: true });
                        preview.appendChild(image);
                    } else {
                        preview.classList.add("library-card-placeholder");
                        preview.textContent = title.slice(0, 1).toUpperCase();
                    }

                    const body = document.createElement("div");
                    body.className = "library-card-body";
                    const heading = document.createElement("h3");
                    const titleLink = document.createElement(linkable ? "a" : "span");
                    titleLink.className = "library-card-title";
                    titleLink.textContent = title;
                    if (stem) titleLink.title = stem;
                    if (linkable) {
                        titleLink.href = openUrl;
                        titleLink.dataset.libraryArticle = "true";
                        titleLink.setAttribute("aria-label", `Open ${title}`);
                    }
                    heading.appendChild(titleLink);
                    body.appendChild(heading);
                    const description =
                        source.long_description ||
                        source.description ||
                        (available
                            ? linkable
                                ? ""
                                : "This source has no homepage and cannot be searched yet."
                            : source.error || "This source cannot be opened.");
                    if (description) {
                        const text = document.createElement("p");
                        text.className = "library-card-description";
                        text.textContent = description;
                        body.appendChild(text);
                    }
                    const details = document.createElement("p");
                    details.className = "library-card-details";
                    const detailValues = [
                        libraryLanguage(source),
                        formatLibrarySize(source.size),
                        Number(source.article_count) > 0
                            ? `${Number(source.article_count).toLocaleString()} ${
                                  Number(source.article_count) === 1
                                      ? "article"
                                      : "articles"
                              }`
                            : "",
                        source.date || "",
                    ].filter(Boolean);
                    details.textContent = detailValues.join(" · ");
                    details.hidden = !detailValues.length;
                    body.appendChild(details);
                    if (source.creator) {
                        const creator = document.createElement("p");
                        creator.className = "library-card-creator";
                        creator.textContent = `By ${source.creator}`;
                        body.appendChild(creator);
                    }
                    const actions = document.createElement("div");
                    actions.className = "library-card-actions";
                    if (!available) {
                        const status = document.createElement("span");
                        status.className = "secondary library-card-action";
                        status.textContent = "Unavailable";
                        actions.appendChild(status);
                    }
                    const detailsButton = document.createElement("button");
                    detailsButton.type = "button";
                    detailsButton.className = "library-button library-card-action";
                    detailsButton.textContent = "Details";
                    detailsButton.addEventListener("click", () => showLibraryInfo(source));
                    actions.appendChild(detailsButton);
                    body.appendChild(actions);
                    card.append(preview, body);
                    grid.appendChild(card);
                });
            }

            function resultKey(item) {
                return item.id || `${item.source_key}:${item.path || item.url || item.title}`;
            }

            function queryTerms(text) {
                return (String(text).toLocaleLowerCase().match(/[\p{L}\p{N}\p{M}]+/gu) || []).filter(
                    (word) => !HIGHLIGHT_STOPWORDS.has(word),
                );
            }

            function highlight(text, terms) {
                const value = String(text);
                const wanted = [
                    ...new Set(
                        (Array.isArray(terms) ? terms : [])
                            .map((term) => String(term || "").toLocaleLowerCase())
                            .filter(Boolean),
                    ),
                ].sort((left, right) => right.length - left.length);
                if (!wanted.length) return escapeHtml(value);
                const pattern = wanted
                    .map((term) => {
                        const safe = term.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
                        // Keep word matching for Latin scripts, but allow substrings
                        // for scripts without word separators (and inflected forms).
                        return /^[a-z0-9]+$/i.test(term) ? `\\b${safe}\\b` : safe;
                    })
                    .join("|");
                let matcher;
                try {
                    matcher = new RegExp(`(${pattern})`, "giu");
                } catch (_) {
                    return escapeHtml(value);
                }
                return value
                    .split(matcher)
                    .map((part, index) =>
                        index % 2
                            ? `<mark>${escapeHtml(part)}</mark>`
                            : escapeHtml(part),
                    )
                    .join("");
            }

            function previewBlocks(text) {
                const value = String(text).replace(/\s+/g, " ").trim();
                if (!value) return [];

                const blockCount = value.length >= 760 ? 3 : value.length >= 360 ? 2 : 1;
                if (blockCount === 1) return [value];

                const sentences = [];
                let start = 0;
                for (let index = 0; index < value.length; index++) {
                    if (!/[.!?。！？]/u.test(value[index])) continue;
                    if (index + 1 < value.length && !/\s/u.test(value[index + 1])) continue;
                    sentences.push(value.slice(start, index + 1).trim());
                    start = index + 1;
                }
                if (start < value.length) sentences.push(value.slice(start).trim());

                if (sentences.length >= 2) {
                    const count = Math.min(blockCount, sentences.length);
                    const blocks = [];
                    let cursor = 0;
                    for (let index = 0; index < count; index++) {
                        const remainingSentences = sentences.length - cursor;
                        const remainingBlocks = count - index;
                        const take = Math.ceil(remainingSentences / remainingBlocks);
                        blocks.push(sentences.slice(cursor, cursor + take).join(" "));
                        cursor += take;
                    }
                    return blocks;
                }

                const words = value.split(" ");
                const blocks = [];
                let cursor = 0;
                for (let index = 0; index < blockCount; index++) {
                    const remainingWords = words.length - cursor;
                    const remainingBlocks = blockCount - index;
                    const take = Math.ceil(remainingWords / remainingBlocks);
                    blocks.push(words.slice(cursor, cursor + take).join(" "));
                    cursor += take;
                }
                return blocks;
            }

            function previewEndsWithPunctuation(text) {
                return /[.!?,;:…。！？；，]["')\]]*$/u.test(text);
            }

            function activeSources() {
                return sources.filter((source) => source.available);
            }

            function selectionValues(values) {
                if (values instanceof Set) return [...values].filter(Boolean);
                return (Array.isArray(values) ? values : values ? [values] : []).filter(Boolean);
            }

            function setSelectedSources(values) {
                selectedSourceKeys = new Set(selectionValues(values));
            }

            function searchScopeParams(params, keys) {
                keys.forEach((key) => params.append("zim", key));
            }

            function sourceFilterParams(params) {
                selectedSourceKeys.forEach((source) => params.append("source", source));
            }

            function sourceFilterLayout() {
                const outcomes = [...current.outcomes.values()];
                const outcomeCount = (outcome) => current.counts[outcome.source.key] || 0;
                // A source is a hard failure only when it reported status
                // "error". A "partial" source returned results, so it must stay
                // selectable and show its count rather than an error label.
                const failed = (outcome) => outcome.status === "error";
                const nonEmptyOutcomes = outcomes.filter(
                    (outcome) => failed(outcome) || outcomeCount(outcome) > 0,
                );
                const query = $("sourceFilterSearch").value.trim().toLocaleLowerCase();
                const queryTerms = query.split(/\s+/).filter(Boolean);
                const matching = queryTerms.length
                    ? nonEmptyOutcomes.filter((outcome) => {
                          const searchable = librarySearchText(outcome.source);
                          return queryTerms.every((term) => searchable.includes(term));
                      })
                    : nonEmptyOutcomes;
                const sorted = matching.sort((left, right) => {
                    const countDifference = outcomeCount(right) - outcomeCount(left);
                    return (
                        countDifference ||
                        sourceDisplayName(left.source).localeCompare(
                            sourceDisplayName(right.source),
                        )
                    );
                });
                const topVisible = sorted.slice(
                    0,
                    query
                        ? sourceFilterShowAll
                            ? sorted.length
                            : SOURCE_FILTER_QUERY_LIMIT
                        : SOURCE_FILTER_LIMIT,
                );
                const selectedVisible = outcomes.filter((outcome) =>
                    selectedSourceKeys.has(outcome.source.key),
                );
                const visibleKeys = new Set([
                    ...topVisible.map((outcome) => outcome.source.key),
                    ...selectedVisible.map((outcome) => outcome.source.key),
                ]);
                let otherOutcomes = nonEmptyOutcomes.filter(
                    (outcome) =>
                        !visibleKeys.has(outcome.source.key) &&
                        outcomeCount(outcome) > 0 &&
                        (!query || matching.includes(outcome)),
                );
                const otherCount = otherOutcomes.reduce(
                    (total, outcome) => total + outcomeCount(outcome),
                    0,
                );
                // A lone hidden source is cheaper as a normal chip than as a
                // one-item disclosure. For an unfiltered list, keep the
                // historical one-result promotion too.
                if (
                    (query && otherOutcomes.length === 1) ||
                    (!query && otherCount === 1)
                ) {
                    otherOutcomes.forEach((outcome) => visibleKeys.add(outcome.source.key));
                    otherOutcomes = [];
                }
                if (!query && sourceFilterShowOther) {
                    otherOutcomes.forEach((outcome) =>
                        visibleKeys.add(outcome.source.key),
                    );
                    otherOutcomes = [];
                }
                const showOther = query
                    ? otherOutcomes.length > 0
                    : otherCount > 1 && otherOutcomes.length > 0;
                return {
                    outcomes,
                    query,
                    matching,
                    visible: [
                        ...selectedVisible,
                        ...sorted.filter(
                            (outcome) =>
                                visibleKeys.has(outcome.source.key) &&
                                !selectedSourceKeys.has(outcome.source.key),
                        ),
                    ],
                    otherOutcomes: showOther ? otherOutcomes : [],
                    otherCount,
                    showOther,
                    failed,
                    outcomeCount,
                };
            }

            function sourceFilterControlKey(node) {
                if (!(node instanceof HTMLElement)) return "";
                if (node.dataset.sourceAction) return `action:${node.dataset.sourceAction}`;
                if (node.dataset.source) return `source:${node.dataset.source}`;
                return "";
            }

            function renderSourceFilters() {
                const panel = $("sourceFilters");
                const toolbar = $("sourceFilterToolbar");
                const options = $("sourceFilterOptions");
                const status = $("sourceFilterStatus");
                if (!current) {
                    panel.hidden = true;
                    panel.classList.remove("empty-selection");
                    toolbar.hidden = true;
                    options.replaceChildren();
                    status.textContent = "";
                    return;
                }
                const focused = document.activeElement;
                const focusKey = options.contains(focused)
                    ? sourceFilterControlKey(focused)
                    : "";
                panel.classList.remove("empty-selection");
                const layout = sourceFilterLayout();
                panel.hidden = false;
                toolbar.hidden =
                    !layout.query && !layout.showOther && !sourceFilterShowOther;

                const all = document.createElement("button");
                all.className = "secondary source-filter-option";
                all.type = "button";
                all.dataset.source = "";
                all.dataset.sourceAction = "all";
                all.setAttribute("aria-pressed", selectedSourceKeys.size ? "false" : "true");
                all.textContent = "All Sources";

                const entries = layout.visible.map((outcome) => {
                    // Counts come from the ranked pool (server-reported), not per-source
                    // candidate lists, so they match what the filtered view actually shows.
                    const count = layout.outcomeCount(outcome);
                    const isFailed = layout.failed(outcome);
                    const button = document.createElement("button");
                    button.className = isFailed
                        ? "source-filter-option err"
                        : count > 0
                          ? "source-filter-option ok"
                          : "source-filter-option";
                    button.type = "button";
                    button.dataset.source = outcome.source.key;
                    const sourceLabel = sourceDisplayName(outcome.source);
                    button.textContent = isFailed
                        ? `${sourceLabel}: ${outcome.error}`
                        : `${sourceLabel}: ${count}`;
                    const stem = sourceStemName(outcome.source);
                    if (stem) button.title = stem;
                    button.disabled = isFailed;
                    button.setAttribute(
                        "aria-pressed",
                        selectedSourceKeys.has(outcome.source.key) ? "true" : "false",
                    );
                    return button;
                });
                if (layout.showOther) {
                    const other = document.createElement("button");
                    other.className = "source-filter-option ok";
                    other.type = "button";
                    other.dataset.sourceAction = layout.query ? "more" : "other";
                    if (!layout.query) {
                        other.setAttribute("aria-pressed", "false");
                    }
                    if (layout.query) {
                        other.textContent = `Show ${layout.otherOutcomes.length} more`;
                    } else {
                        const count = layout.otherOutcomes.length;
                        other.textContent = `Other: ${count} source${count === 1 ? "" : "s"}`;
                        other.title =
                            `Show ${count} more sources ` +
                            `(${layout.otherCount} results)`;
                    }
                    entries.push(other);
                }
                options.replaceChildren(all, ...entries);
                if (focusKey) {
                    const buttons = [...options.querySelectorAll("button")];
                    const target =
                        buttons.find(
                            (button) =>
                                !button.disabled &&
                                sourceFilterControlKey(button) === focusKey,
                        ) ||
                        buttons.find(
                            (button) =>
                                !button.disabled &&
                                button.getAttribute("aria-pressed") === "true",
                        ) ||
                        buttons.find((button) => !button.disabled);
                    if (target) target.focus({ preventScroll: true });
                }

                const visibleMatching = layout.visible.filter((outcome) =>
                    layout.matching.includes(outcome),
                ).length;
                if (!layout.matching.length) {
                    status.textContent = layout.query
                        ? "No matching sources."
                        : layout.outcomes.length
                          ? ""
                          : "No sources have reported yet.";
                } else if (
                    layout.query &&
                    layout.matching.length > visibleMatching
                ) {
                    status.textContent =
                        `Showing ${visibleMatching} of ${layout.matching.length} ` +
                        "matching sources.";
                } else {
                    status.textContent = "";
                }
            }

            function renderSourceSelectionSummary() {
                const panel = $("sourceFilters");
                const toolbar = $("sourceFilterToolbar");
                const options = $("sourceFilterOptions");
                const status = $("sourceFilterStatus");
                if (current || !selectedSourceKeys.size) {
                    if (!current) {
                        panel.hidden = true;
                        panel.classList.remove("empty-selection");
                        toolbar.hidden = true;
                        options.replaceChildren();
                        status.textContent = "";
                    }
                    return;
                }
                panel.hidden = false;
                panel.classList.add("empty-selection");
                toolbar.hidden = true;
                const all = document.createElement("button");
                all.className = "secondary source-filter-option";
                all.type = "button";
                all.dataset.sourceAction = "all";
                all.setAttribute("aria-pressed", "false");
                all.textContent = "All Sources";
                const entries = [...selectedSourceKeys].map((key) => {
                    const button = document.createElement("button");
                    button.className = "source-filter-option ok";
                    button.type = "button";
                    button.dataset.source = key;
                    button.setAttribute("aria-pressed", "true");
                    button.textContent = sourceDisplayName(key);
                    const stem = sourceStemName(key);
                    if (stem) button.title = stem;
                    return button;
                });
                options.replaceChildren(all, ...entries);
                const count = entries.length;
                status.textContent =
                    `${count} selected source${count === 1 ? "" : "s"}. ` +
                    `Type a query to search ${count === 1 ? "it" : "them"}.`;
            }

            function renderFilterLine(total) {
                $("filterLine").innerHTML = total > 0
                    ? '<hr class="source-results-break" aria-hidden="true">'
                    : "";
            }

            function setSourceMeta(node, item) {
                const meta = node.querySelector(".result-meta");
                meta.replaceChildren();
                if (item.source_icon) {
                    const icon = document.createElement("img");
                    icon.className = "result-source-icon";
                    icon.alt = "";
                    icon.width = 16;
                    icon.height = 16;
                    icon.loading = "lazy";
                    icon.decoding = "async";
                    icon.src = item.source_icon;
                    icon.addEventListener("error", () => icon.remove(), { once: true });
                    meta.appendChild(icon);
                }
                const source = document.createElement("span");
                const sourceKey = item.source_key || item.source || "";
                source.textContent = sourceDisplayName(sourceKey);
                const stem = sourceStemName(sourceKey);
                if (stem) source.title = stem;
                if (sourceKey) source.dataset.sourceKey = sourceKey;
                meta.appendChild(source);
                const separator = document.createElement("span");
                separator.className = "result-meta-separator";
                separator.setAttribute("aria-hidden", "true");
                separator.hidden = true;
                separator.textContent = "›";
                meta.appendChild(separator);
                const url = document.createElement("span");
                url.className = "result-url";
                url.hidden = true;
                meta.appendChild(url);
            }

            function resultUrlLabel(item) {
                const rawUrl = String(item.url || "").trim();
                const rawPath = String(item.path || "").trim();
                if (!rawUrl && !rawPath) return "";

                const decodeSegment = (segment) => {
                    try {
                        return decodeURIComponent(segment);
                    } catch (_) {
                        return segment;
                    }
                };
                const segments = (value) => value.split("/").filter(Boolean).map(decodeSegment);

                if (!rawUrl) {
                    return segments(rawPath).join(" › ");
                }

                try {
                    const parsed = new URL(rawUrl, location.href);
                    const path = segments(stripBasePath(parsed.pathname));
                    if (parsed.origin === location.origin && (path[0] === "article" || path[0] === "content")) {
                        // Both sides are decoded exactly once: rawPath by
                        // segments(), otherwise the already-decoded URL path.
                        return (rawPath ? segments(rawPath) : path.slice(2)).join(" › ");
                    }
                    return [`${parsed.protocol}//${parsed.host}`, ...path].join(" › ");
                } catch (_) {
                    return segments(rawPath).join(" › ");
                }
            }

            function setResultUrl(node, item) {
                const separator = node.querySelector(".result-meta-separator");
                const url = node.querySelector(".result-url");
                const label = resultUrlLabel(item);
                separator.hidden = !label;
                url.textContent = label;
                url.hidden = !label;
            }

            function isHtmlMimetype(mimetype) {
                const mime = String(mimetype || "").toLowerCase();
                return mime === "text/html" || mime === "application/xhtml+xml";
            }

            function mediaBadge(mimetype) {
                const mime = String(mimetype || "").toLowerCase();
                if (!mime || mime === "text/html" || mime === "application/xhtml+xml") return null;
                if (mime.startsWith("video/")) return { icon: "▶", label: "Video" };
                if (mime.startsWith("audio/")) return { icon: "♪", label: "Audio" };
                if (mime.startsWith("image/")) return { icon: "IMG", label: "Image" };
                if (mime === "application/pdf") return { icon: "PDF", label: "PDF" };
                if (mime.startsWith("text/")) return { icon: "TXT", label: "Text" };
                return { icon: "FILE", label: "File" };
            }

            function appendMediaBadge(title, mimetype) {
                const media = mediaBadge(mimetype);
                if (!media) return;
                const badge = document.createElement("span");
                badge.className = "result-media";
                badge.title = media.label;
                badge.setAttribute("aria-label", media.label);
                badge.textContent = media.icon;
                title.append(" ", badge);
            }

            function modifiedClick(event) {
                return event.button !== 0 || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey;
            }

            function articleUrlValue(articleUrl) {
                return articleUrl.pathname + articleUrl.search + articleUrl.hash;
            }

            function readModalUrl(value) {
                if (!value) return null;
                try {
                    const articleUrl = new URL(value, location.href);
                    const isContent = articleUrl.pathname.startsWith(appPath("/content/"));
                    // /random?zim=... redirects to a /content/ shell. Accept it
                    // while the iframe resolves so a library card without a
                    // homepage can open a random article.
                    const isRandom = articleUrl.pathname === appPath("/random");
                    if (
                        articleUrl.origin !== location.origin ||
                        (!isContent && !isRandom)
                    ) {
                        return null;
                    }
                    return articleUrl;
                } catch (_) {
                    return null;
                }
            }

            // The modal loads a trusted /content/ shell; the article inside it is
            // sandboxed only when secure browsing is enabled. The shell reports the
            // viewed article's title and favicon through postMessage and we mirror
            // them on the browser tab.
            function applyArticleDocumentMeta(title, icon) {
                const articleTitle = String(title || "").trim();
                if (articleTitle) document.title = articleTitle;
                if (!icon) {
                    restoreDefaultIcons();
                    return;
                }
                if (!articleIconsHidden) {
                    defaultIconLinks.forEach((link) => link.remove());
                    articleIconsHidden = true;
                }
                if (!articleIconLink) {
                    articleIconLink = document.createElement("link");
                    articleIconLink.rel = "icon";
                }
                articleIconLink.href = icon;
                document.head.appendChild(articleIconLink);
            }

            function restoreDefaultIcons() {
                if (articleIconLink) {
                    articleIconLink.remove();
                    articleIconLink = null;
                }
                if (articleIconsHidden) {
                    defaultIconLinks.forEach((link) => document.head.appendChild(link));
                    articleIconsHidden = false;
                }
            }

            function resetArticleDocumentMeta() {
                document.title = defaultDocumentTitle;
                restoreDefaultIcons();
            }

            function readExternalUrl(value) {
                try {
                    const url = new URL(value, location.href);
                    if (
                        (url.protocol !== "http:" && url.protocol !== "https:") ||
                        url.origin === location.origin
                    ) {
                        return null;
                    }
                    return url;
                } catch (_) {
                    return null;
                }
            }

            function hideExternalLinkNotice() {
                offlineLinkRequest++;
                const notice = $("externalLinkNotice");
                const frame = $("articleModalFrame");
                const wrap = document.querySelector(".wrap");
                if (notice) notice.hidden = true;
                if (frame) frame.inert = false;
                // The notice is only one layer. If the article modal is still
                // open, the page behind it must stay inert; otherwise closing
                // the notice lets Tab escape into the hidden page.
                const articleModal = $("articleModal");
                if (wrap) wrap.inert = Boolean(articleModal && !articleModal.hidden);
                if (frame && articleModal && !articleModal.hidden) frame.focus();
            }

            async function copyTextToClipboard(text) {
                try {
                    if (navigator.clipboard?.writeText) {
                        await navigator.clipboard.writeText(text);
                        return true;
                    }
                } catch (_) {
                    // Sandboxed or permission-restricted contexts fall through to
                    // the selection-based fallback below.
                }
                const area = document.createElement("textarea");
                area.value = text;
                area.setAttribute("readonly", "");
                area.style.position = "fixed";
                area.style.opacity = "0";
                area.style.pointerEvents = "none";
                const previous = document.activeElement;
                document.body.appendChild(area);
                area.focus();
                area.select();
                let copied = false;
                try {
                    copied = document.execCommand("copy");
                } catch (_) {
                    copied = false;
                } finally {
                    area.remove();
                    if (previous instanceof HTMLElement && document.contains(previous)) {
                        previous.focus();
                    }
                }
                return copied;
            }

            function showExternalLinkNotice(value) {
                const url = readExternalUrl(value);
                const modal = $("articleModal");
                const notice = $("externalLinkNotice");
                const frame = $("articleModalFrame");
                const link = $("externalLinkOpen");
                const urlLabel = $("externalLinkUrl");
                const wrap = document.querySelector(".wrap");
                if (!url || !modal || !notice || !frame || !link || !urlLabel) return;
                link.href = url.href;
                urlLabel.textContent = url.href;
                notice.hidden = false;
                // The article iframe is behind this notice and must not take
                // keyboard focus while the notice is showing.
                frame.inert = true;
                if (wrap) wrap.inert = true;
                notice.focus();
            }

            async function handleExternalLink(value, offlineValue, parentValue = null) {
                const url = readExternalUrl(value);
                const offlineUrl = readModalUrl(offlineValue);
                if (!url) return;
                if (!offlineUrl) {
                    showExternalLinkNotice(url.href);
                    return;
                }
                const request = ++offlineLinkRequest;
                const currentArticleUrl = activeModalUrl;
                try {
                    const response = await fetch(offlineUrl.href, {
                        cache: "no-store",
                        credentials: "same-origin",
                    });
                    if (!response.ok) throw new Error(`HTTP ${response.status}`);
                    if (
                        request !== offlineLinkRequest ||
                        $("articleModal").hidden ||
                        activeModalUrl !== currentArticleUrl
                    ) {
                        return;
                    }
                    const resolvedUrl = readModalUrl(response.url) || offlineUrl;
                    resolvedUrl.search = offlineUrl.search;
                    resolvedUrl.hash = offlineUrl.hash;
                    navigateArticleModal(resolvedUrl, "follow", {
                        parentUrl: readModalUrl(parentValue),
                    });
                } catch (_) {
                    if (request === offlineLinkRequest) showExternalLinkNotice(url.href);
                }
            }

            function showArticleModal(articleUrl, navigationKind = "manual", parentUrl = null) {
                const modal = $("articleModal");
                const frame = $("articleModalFrame");
                if (!modal || !frame) return;
                const wasHidden = modal.hidden;
                const fixedParentUrl = readModalUrl(parentUrl);
                if (navigationKind === "library") {
                    activeLibraryArticleUrl = articleUrl;
                } else if (navigationKind !== "parent") {
                    activeLibraryArticleUrl = null;
                }
                hideExternalLinkNotice();
                if (wasHidden) {
                    modalPreviousFocus = document.activeElement;
                }
                const previousActiveArticleUrl = readModalUrl(activeModalUrl);
                const shouldLoad = activeModalUrl !== articleUrl;
                activeModalUrl = articleUrl;
                if (shouldLoad) {
                    previousModalArticleUrl = previousActiveArticleUrl?.href || null;
                    pendingModalArticleUrl = articleUrl;
                    pendingModalNavigation = {
                        url: articleUrl,
                        kind: navigationKind,
                        parentUrl: fixedParentUrl ? fixedParentUrl.href : null,
                    };
                    if (navigationKind !== "parent") {
                        activeParentNavigation = null;
                        parentNavigationSequence++;
                    }
                    if (!wasHidden && frame.contentWindow) {
                        // An already-open shell can replace its own document
                        // without adding another joint-history entry for the
                        // iframe. The first load still uses src.
                        frame.contentWindow.postMessage({
                            type: "zimantic-shell-navigate",
                            url: articleUrl,
                        }, "*");
                    } else {
                        frame.src = articleUrl;
                    }
                }
                modal.hidden = false;
                document.documentElement.classList.add("modal-open");
                document.body.classList.add("modal-open");
                // Keep the page behind the article modal out of the tab order
                // and the accessibility tree while the dialog is open.
                const wrap = document.querySelector(".wrap");
                if (wrap) wrap.inert = true;
                frame.focus();
            }

            function modalStateEntry(articleUrl, navigationKind, parentUrl = null) {
                const locationState = readLocationState();
                const state = {
                    ...(history.state || {}),
                    zimanticModal: true,
                    zimanticModalNavigation: navigationKind,
                    zimanticLinkedArticle: articleRouteValue(articleUrl),
                };
                const parent = readModalUrl(parentUrl);
                if (parent) {
                    state.zimanticParent = articleUrlValue(parent);
                } else {
                    delete state.zimanticParent;
                }
                return {
                    state,
                    url: makeStateUrl(
                        current?.question || locationState.question,
                        current ? selectedSourceKeys : locationState.selectedSources,
                        current?.page ?? locationState.page,
                        articleUrlValue(articleUrl),
                    ),
                };
            }

            function navigateArticleModal(
                articleUrl,
                navigationKind = "search",
                { parentUrl = null } = {},
            ) {
                commitAutoSearchHistory();
                const modalOpen = !$("articleModal").hidden;
                const activeArticleUrl = readModalUrl(activeModalUrl);
                const fixedParentUrl =
                    readModalUrl(navigationKind === "parent" ? parentUrl || activeParentUrl : parentUrl);
                const entry = modalStateEntry(articleUrl, navigationKind, fixedParentUrl);
                // Each article gets its own history entry, so Back/Forward walks
                // through everything the reader visited. Close only drops the
                // modal from the current entry; it does not discard that trail.
                const newArticle =
                    !modalOpen ||
                    !activeArticleUrl ||
                    articleUrlValue(activeArticleUrl) !== articleUrlValue(articleUrl);
                if (newArticle) {
                    history.pushState(entry.state, "", entry.url);
                } else {
                    history.replaceState(entry.state, "", entry.url);
                }
                showArticleModal(articleUrl.href, navigationKind, fixedParentUrl);
            }

            function openArticleModal(link) {
                const articleUrl = new URL(link.href, location.href);
                rememberSearchResult(link);
                navigateArticleModal(articleUrl, "search");
            }

            function articleResultLinks() {
                return [...$("results").querySelectorAll(".result-title a[href]")].filter((link) => {
                    if (link.dataset.mimetype && !isHtmlMimetype(link.dataset.mimetype)) return false;
                    try {
                        const articleUrl = new URL(link.href, location.href);
                        return (
                            articleUrl.origin === location.origin &&
                            articleUrl.pathname.startsWith(appPath("/content/"))
                        );
                    } catch (_) {
                        return false;
                    }
                });
            }

            function libraryArticleLinks() {
                return [...$("libraryGrid").querySelectorAll("article.library-card")].map((card) =>
                    card.querySelector("a[data-library-article][href]"),
                ).filter(Boolean);
            }

            function libraryModalNavigation() {
                const unavailable = {
                    previous: null,
                    next: null,
                    previousAvailable: false,
                    nextAvailable: false,
                };
                if (!activeLibraryArticleUrl) return unavailable;
                const links = libraryArticleLinks();
                const currentValue = articleUrlValue(new URL(activeLibraryArticleUrl, location.href));
                const index = links.findIndex((link) => {
                    try {
                        return articleUrlValue(new URL(link.href, location.href)) === currentValue;
                    } catch (_) {
                        return false;
                    }
                });
                if (index < 0) return unavailable;
                const urlFor = (offset) => {
                    const link = links[index + offset];
                    return link ? articleUrlValue(new URL(link.href, location.href)) : null;
                };
                const previous = urlFor(-1);
                const next = urlFor(1);
                return {
                    previous,
                    next,
                    previousAvailable: Boolean(previous),
                    nextAvailable: Boolean(next),
                };
            }

            function rememberSearchResult(link) {
                if (!current || !link) return false;
                const articleUrl = readModalUrl(link.href);
                if (!articleUrl) return false;
                if (!articleResultLinks().includes(link)) return false;
                activeSearchResult = {
                    page: current.page,
                    url: articleUrlValue(articleUrl),
                };
                return true;
            }

            function rememberSearchResultUrl(url, page = current?.page) {
                if (!current || !Number.isInteger(page) || page < 0) return false;
                const articleUrl = readModalUrl(url);
                if (!articleUrl || page !== current.page) return false;
                const value = articleUrlValue(articleUrl);
                const isResult = articleResultLinks().some((link) => {
                    try {
                        return articleUrlValue(new URL(link.href, location.href)) === value;
                    } catch (_) {
                        return false;
                    }
                });
                if (!isResult) return false;
                activeSearchResult = { page, url: value };
                return true;
            }

            function articleModalNavigation() {
                const unavailable = {
                    previous: null,
                    next: null,
                    previousAvailable: false,
                    nextAvailable: false,
                };
                if (!activeSearchResult || !current) {
                    return location.pathname === appPath("/library")
                        ? libraryModalNavigation()
                        : unavailable;
                }
                const pageCount = Math.max(1, Math.ceil(current.total / PAGE_SIZE));
                if (activeSearchResult.page !== current.page) {
                    return {
                        ...unavailable,
                        previousAvailable: activeSearchResult.page > 0,
                        nextAvailable: activeSearchResult.page < pageCount - 1,
                    };
                }
                const links = articleResultLinks();
                const index = links.findIndex((link) => {
                    try {
                        return articleUrlValue(new URL(link.href, location.href)) === activeSearchResult.url;
                    } catch (_) {
                        return false;
                    }
                });
                if (index < 0) {
                    return {
                        ...unavailable,
                        previousAvailable: activeSearchResult.page > 0,
                        nextAvailable: activeSearchResult.page < pageCount - 1,
                    };
                }
                const urlFor = (offset) => {
                    const link = links[index + offset];
                    return link ? articleUrlValue(new URL(link.href, location.href)) : null;
                };
                const previous = urlFor(-1);
                const next = urlFor(1);
                return {
                    previous,
                    next,
                    previousAvailable: Boolean(previous) || activeSearchResult.page > 0,
                    nextAvailable: Boolean(next) || activeSearchResult.page < pageCount - 1,
                };
            }

            async function navigateSearchResult(direction, requestedUrl = null) {
                const navigation = articleModalNavigation();
                if (requestedUrl) {
                    const articleUrl = readModalUrl(requestedUrl);
                    if (!articleUrl || articleUrlValue(articleUrl) !== navigation[direction]) return;
                    if (location.pathname === appPath("/library") && !activeSearchResult) {
                        navigateArticleModal(articleUrl, "library");
                        return;
                    }
                    if (!rememberSearchResultUrl(articleUrl.href)) return;
                    navigateArticleModal(articleUrl, "search");
                    return;
                }
                if (!activeSearchResult || !current) return;
                const pageCount = Math.max(1, Math.ceil(current.total / PAGE_SIZE));
                const page = activeSearchResult.page + (direction === "next" ? 1 : -1);
                if (page < 0 || page >= pageCount) return;
                const sequence = searchSequence;
                await loadPage(page);
                if (sequence !== searchSequence || !current || current.page !== page) return;
                const links = articleResultLinks();
                const link = direction === "next" ? links[0] : links[links.length - 1];
                if (!link || !rememberSearchResult(link)) return;
                navigateArticleModal(new URL(link.href, location.href), "search");
            }

            function articleRouteParts(articleUrl) {
                const parts = stripBasePath(articleUrl.pathname).split("/").filter(Boolean);
                return parts[0] === "article" && parts.length >= 3 ? parts : null;
            }

            // The modal frame loads a /content/ toolbar shell; its inner iframe loads the
            // raw entry from /article/. These helpers convert between the two so parent
            // navigation and thumbnails can read the raw article while the UI keeps using
            // the shell (toolbar-bearing) URLs.
            function rawArticleUrl(articleUrl) {
                try {
                    const url = new URL(articleUrl?.href || String(articleUrl), location.href);
                    const pathname = stripBasePath(url.pathname);
                    if (url.origin !== location.origin || !pathname.startsWith("/content/")) {
                        return null;
                    }
                    const raw = new URL(url.href);
                    raw.pathname = appPath(`/article${pathname.slice("/content".length)}`);
                    return raw;
                } catch (_) {
                    return null;
                }
            }

            function contentUrlFromRaw(rawUrl) {
                try {
                    const url = new URL(rawUrl, location.href);
                    const pathname = stripBasePath(url.pathname);
                    if (url.origin !== location.origin || !pathname.startsWith("/article/")) {
                        return null;
                    }
                    const content = new URL(url.href);
                    content.pathname = appPath(`/content${pathname.slice("/article".length)}`);
                    return content;
                } catch (_) {
                    return null;
                }
            }

            function parentArticleUrls(articleUrl) {
                const pathname = stripBasePath(articleUrl.pathname);
                const parts = pathname.split("/").filter(Boolean);
                if (parts[0] !== "content" || parts.length < 3) return [];
                // A capture's article URL can end in a slug (questions/2219/title)
                // or any other nested path. Walk up toward the ZIM root until a
                // page is found that actually links to this article.
                const parents = [];
                while (parts.length > 2) {
                    parts.pop();
                    parents.push(new URL(appPath(parts.join("/")), location.origin));
                }
                return parents;
            }

            function articleRouteValue(articleUrl) {
                return articleUrl.pathname + articleUrl.search;
            }

            function readParentArticleLinks(html, rawParentUrl) {
                const document = new DOMParser().parseFromString(html, "text/html");
                const parentParts = articleRouteParts(rawParentUrl);
                if (!parentParts) return null;
                const zim = parentParts[1];
                const parentContentUrl = contentUrlFromRaw(rawParentUrl);
                if (!parentContentUrl) return null;
                const parentValue = articleUrlValue(parentContentUrl);
                const links = [];
                const seen = new Set();
                for (const link of document.querySelectorAll("a[href]")) {
                    if (link.closest("[data-zimantic-toolbar]")) continue;
                    const href = link.getAttribute("href");
                    if (!href || href.startsWith("#")) continue;
                    let linkUrl;
                    try {
                        linkUrl = new URL(href, rawParentUrl);
                    } catch (_) {
                        continue;
                    }
                    const linkParts = articleRouteParts(linkUrl);
                    if (linkUrl.origin !== location.origin || !linkParts || linkParts[1] !== zim) {
                        continue;
                    }
                    const contentUrl = contentUrlFromRaw(linkUrl);
                    if (!contentUrl) continue;
                    const value = articleUrlValue(contentUrl);
                    if (value === parentValue) continue;
                    if (seen.has(value)) continue;
                    seen.add(value);
                    links.push(contentUrl);
                }
                return links;
            }

            function rememberArticleAlias(articleUrl, linkedUrl) {
                const article = readModalUrl(articleUrl);
                const linked = readModalUrl(linkedUrl);
                if (!article || !linked) return;
                const articleValue = articleRouteValue(article);
                const linkedValue = articleRouteValue(linked);
                if (articleValue === linkedValue) return;
                let aliases = articleAliases.get(articleValue);
                if (!aliases) {
                    aliases = new Set();
                    articleAliases.set(articleValue, aliases);
                }
                aliases.add(linkedValue);
            }

            function parentNavigationAt(links, articleUrl) {
                if (!links) return null;
                const currentValue = articleRouteValue(articleUrl);
                let index = links.findIndex(
                    (link) => articleRouteValue(link) === currentValue,
                );
                if (index < 0) {
                    // Parent links use the URL the capture authored, which can
                    // redirect (Internet_connection → Internet_access). Match via
                    // the link that actually got us here when that happened.
                    const aliases = articleAliases.get(currentValue);
                    if (aliases) {
                        index = links.findIndex((link) =>
                            aliases.has(articleRouteValue(link)),
                        );
                    }
                }
                if (index < 0) return null;
                if (!links[index - 1] && !links[index + 1]) return null;
                const urlAt = (offset) => {
                    const link = links[index + offset];
                    return link ? articleUrlValue(link) : null;
                };
                return {
                    previous: urlAt(-1),
                    next: urlAt(1),
                };
            }

            async function fetchParentArticleLinks(parentUrl) {
                const key = articleRouteValue(parentUrl);
                const cached = parentNavigationCache.get(key);
                if (cached) return cached;
                const rawParentUrl = rawArticleUrl(parentUrl);
                if (!rawParentUrl) return null;
                const pending = (async () => {
                    try {
                        const response = await fetch(rawParentUrl);
                        if (!response.ok) throw new Error(`HTTP ${response.status}`);
                        const html = await response.text();
                        return {
                            responseUrl: response.url ? new URL(response.url) : null,
                            links: readParentArticleLinks(html, rawParentUrl),
                        };
                    } catch (_) {
                        parentNavigationCache.delete(key);
                        return null;
                    }
                })();
                parentNavigationCache.set(key, pending);
                return pending;
            }

            async function parentNavigationFor(parentUrl, articleUrl) {
                const info = await fetchParentArticleLinks(parentUrl);
                if (!info) return null;
                const rawArticle = rawArticleUrl(articleUrl);
                if (
                    info.responseUrl &&
                    rawArticle &&
                    articleRouteValue(info.responseUrl) === articleRouteValue(rawArticle)
                ) {
                    return null;
                }
                return parentNavigationAt(info.links, articleUrl);
            }

            async function loadParentArticleNavigation(
                articleUrl,
                { parentUrl = null, allowFallback = true } = {},
            ) {
                const request = ++parentNavigationSequence;
                activeParentNavigation = null;
                activeParentUrl = null;
                postArticleModalNavigation();
                const preferredUrl = readModalUrl(parentUrl);
                const candidates = preferredUrl ? [preferredUrl] : [];
                if (allowFallback) candidates.push(...parentArticleUrls(articleUrl));
                const seenParents = new Set();
                for (const candidateParentUrl of candidates) {
                    if (!candidateParentUrl) continue;
                    const parentValue = articleRouteValue(candidateParentUrl);
                    if (seenParents.has(parentValue)) continue;
                    seenParents.add(parentValue);
                    const navigation = await parentNavigationFor(candidateParentUrl, articleUrl);
                    if (request !== parentNavigationSequence || activeModalUrl !== articleUrl.href) {
                        return;
                    }
                    if (!navigation) continue;
                    activeParentNavigation = navigation;
                    activeParentUrl = candidateParentUrl.href;
                    break;
                }
                if (request !== parentNavigationSequence) return;
                postArticleModalNavigation();
            }

            function postArticleModalNavigation() {
                const frame = $("articleModalFrame");
                if (!frame?.contentWindow || $("articleModal").hidden) return;
                frame.contentWindow.postMessage(
                    {
                        type: "zimantic-modal-navigation",
                        modal: true,
                        parent: activeParentNavigation,
                        ...articleModalNavigation(),
                    },
                    "*",
                );
            }

            function debugNumber(value, digits = 6) {
                const number = Number(value);
                return Number.isFinite(number)
                    ? number.toFixed(digits).replace(/0+$/, "").replace(/\.$/, "")
                    : "—";
            }

            function appendDebugField(list, label, value) {
                const term = document.createElement("dt");
                term.textContent = label;
                const description = document.createElement("dd");
                description.textContent = value;
                list.append(term, description);
            }

            function setResultDebug(node, item) {
                let details = node.querySelector(".result-debug");
                const ranking = item.ranking;
                if (!debugMode || !ranking || typeof ranking !== "object") {
                    if (details) details.remove();
                    return;
                }
                const wasOpen = Boolean(details?.open);
                if (!details) {
                    details = document.createElement("details");
                    details.className = "result-debug";
                    node.querySelector(".result-body").appendChild(details);
                }
                details.replaceChildren();
                details.open = wasOpen;
                const summary = document.createElement("summary");
                summary.textContent = `Rank #${ranking.final_rank ?? "—"} · score ${debugNumber(item.score)}`;
                details.appendChild(summary);

                const grid = document.createElement("dl");
                grid.className = "result-debug-grid";
                appendDebugField(grid, "source rank", String(ranking.source_rank ?? item.rank ?? "—"));
                appendDebugField(grid, "RRF total", debugNumber(ranking.rrf_score));
                const rrf = ranking.rrf && typeof ranking.rrf === "object" ? ranking.rrf : {};
                const methods = Object.entries(rrf)
                    .filter(([, value]) => value && value.rank != null)
                    .map(
                        ([name, value]) =>
                            `${name} #${value.rank} (${debugNumber(value.score)} × ${value.weight} → ${debugNumber(value.contribution)})`,
                    );
                appendDebugField(grid, "matches", methods.join(", ") || "none");
                const title = ranking.title && typeof ranking.title === "object" ? ranking.title : {};
                appendDebugField(
                    grid,
                    "title",
                    `${title.matches ?? 0}/${title.term_count ?? 0} terms, ` +
                        `${Math.round(Number(title.coverage || 0) * 100)}% coverage, ` +
                        `${Math.round(Number(title.density || 0) * 100)}% density`,
                );
                appendDebugField(
                    grid,
                    "signals",
                    `lexical ${debugNumber(ranking.lexical_score)}, ` +
                        `phrase ${ranking.phrase ? "yes" : "no"}, ` +
                        `intent ${ranking.intent ? "yes" : "no"}`,
                );
                const disambiguation = ranking.disambiguation;
                if (disambiguation?.action && disambiguation.action !== "none") {
                    appendDebugField(
                        grid,
                        "disambiguation",
                        `${disambiguation.action} (${debugNumber(disambiguation.adjustment)})`,
                    );
                }
                details.appendChild(grid);
            }

            function renderMatchedBy() {
                const line = $("debugStats");
                if (!line) return;
                if (!debugMode || !current?.matchedBy) {
                    line.hidden = true;
                    line.textContent = "";
                    return;
                }
                const matched = current.matchedBy;
                line.textContent =
                    `Matched by: semantic ${matched.semantic ?? 0}, title ${matched.title ?? 0}, ` +
                    `full text ${matched.fulltext ?? 0}`;
                line.hidden = false;
            }

            function bindArticleFrameLoad(frame = $("articleModalFrame")) {
                if (!frame) return;
                frame.addEventListener("load", () => {
                    const modal = $("articleModal");
                    const activeFrame = $("articleModalFrame");
                    if (modal.hidden || !activeFrame.src || activeFrame.src === "about:blank") {
                        return;
                    }
                    postArticleModalNavigation();
                });
            }

            function replaceArticleFrame() {
                const old = $("articleModalFrame");
                if (!old) return;
                const fresh = old.cloneNode(false);
                fresh.removeAttribute("src");
                old.replaceWith(fresh);
                bindArticleFrameLoad(fresh);
            }

            function hideArticleModal() {
                const modal = $("articleModal");
                if (!modal || modal.hidden) return;
                modal.hidden = true;
                document.documentElement.classList.remove("modal-open");
                document.body.classList.remove("modal-open");
                const wrap = document.querySelector(".wrap");
                if (wrap) wrap.inert = false;
                activeModalUrl = null;
                activeSearchResult = null;
                activeLibraryArticleUrl = null;
                previousModalArticleUrl = null;
                pendingModalArticleUrl = null;
                pendingModalNavigation = null;
                hideExternalLinkNotice();
                activeParentNavigation = null;
                activeParentUrl = null;
                parentNavigationSequence++;
                resetArticleDocumentMeta();
                replaceArticleFrame();
                if (modalPreviousFocus instanceof HTMLElement && document.contains(modalPreviousFocus)) {
                    modalPreviousFocus.focus();
                }
                modalPreviousFocus = null;
            }

            function closeArticleModal({ navigateHistory = true } = {}) {
                const modal = $("articleModal");
                if (!modal || modal.hidden) return;
                if (navigateHistory && new URL(location.href).searchParams.has("modal")) {
                    // Close is a local action: clear the modal from this entry
                    // while leaving the reader's earlier articles in history.
                    const url = new URL(location.href);
                    url.searchParams.delete("modal");
                    history.replaceState(history.state, "", url.pathname + url.search + url.hash);
                }
                hideArticleModal();
            }

            bindArticleFrameLoad();
            window.addEventListener("message", (event) => {
                const frame = $("articleModalFrame");
                if (event.source !== frame.contentWindow) return;
                if (event.data?.type === "zimantic-article-copy") {
                    let url;
                    try {
                        url = new URL(String(event.data.text || ""), location.href);
                    } catch (_) {
                        return;
                    }
                    if (url.origin !== location.origin) return;
                    copyTextToClipboard(url.href).then((ok) => {
                        const activeFrame = $("articleModalFrame");
                        if (!activeFrame?.contentWindow) return;
                        activeFrame.contentWindow.postMessage(
                            { type: "zimantic-article-copy-result", ok },
                            "*",
                        );
                    });
                    return;
                }
                if (event.data?.type === "zimantic-search") {
                    const question = String(event.data.query || "")
                        .trim()
                        .slice(0, 4096);
                    if (!question) {
                        closeArticleModal();
                        return;
                    }
                    closeArticleModal({ navigateHistory: false });
                    $("question").value = question;
                    submitSearch(false);
                    return;
                }
                if (event.data?.type === "zimantic-external-link") {
                    handleExternalLink(event.data.url, event.data.offlineUrl, event.data.parent);
                    return;
                }
                if (event.data?.type === "zimantic-app-navigate") {
                    // A same-origin app link from a captured article. The shell
                    // runs with an opaque origin in the modal, so it cannot load
                    // the app itself; navigate this top-level page instead.
                    let url;
                    try {
                        url = new URL(String(event.data.url || ""), location.href);
                    } catch (_) {
                        return;
                    }
                    if (url.origin !== location.origin) return;
                    location.href = url.href;
                    return;
                }
                if (event.data?.type === "zimantic-open-article") {
                    const articleUrl = readModalUrl(event.data.url);
                    if (articleUrl) window.open(articleUrl.href, "_blank", "noopener");
                    return;
                }
                if (event.data?.type === "zimantic-article-close") closeArticleModal();
                if (event.data?.type === "zimantic-article-ready") {
                    const articleUrl = readModalUrl(event.data.url);
                    if (articleUrl) {
                        applyArticleDocumentMeta(event.data.title, event.data.icon);
                        const activeArticleUrl = readModalUrl(activeModalUrl);
                        const pendingArticleUrl = readModalUrl(pendingModalArticleUrl);
                        const pendingNavigation = pendingModalNavigation;
                        const isPendingNavigation = Boolean(pendingNavigation);
                        const pendingParentUrl = readModalUrl(pendingNavigation?.parentUrl);
                        const navigationKind = isPendingNavigation ? pendingNavigation.kind : "manual";
                        if (pendingArticleUrl) {
                            rememberArticleAlias(articleUrl, pendingArticleUrl);
                        }
                        const changedArticle =
                            activeArticleUrl &&
                            articleUrlValue(activeArticleUrl) !== articleUrlValue(articleUrl);
                        if (!isPendingNavigation && changedArticle) {
                            previousModalArticleUrl = activeArticleUrl.href;
                        }
                        activeModalUrl = articleUrl.href;
                        if (!isPendingNavigation && changedArticle) {
                            // A page inside the article navigated its own shell.
                            // Add a history entry for it, remembering that page as
                            // the referrer for parent navigation.
                            const referrer = readModalUrl(previousModalArticleUrl);
                            const entry = modalStateEntry(
                                articleUrl,
                                referrer ? "follow" : "manual",
                                referrer,
                            );
                            history.pushState(entry.state, "", entry.url);
                        } else {
                            syncModalStateUrl(articleUrl);
                        }
                        pendingModalArticleUrl = null;
                        pendingModalNavigation = null;
                        if (pendingParentUrl && navigationKind === "parent") {
                            loadParentArticleNavigation(articleUrl, {
                                parentUrl: pendingParentUrl,
                                allowFallback: false,
                            });
                        } else if (pendingParentUrl && navigationKind === "follow") {
                            loadParentArticleNavigation(articleUrl, {
                                parentUrl: pendingParentUrl,
                            });
                        } else if (navigationKind === "manual") {
                            loadParentArticleNavigation(articleUrl, {
                                parentUrl: readModalUrl(previousModalArticleUrl),
                            });
                        } else {
                            loadParentArticleNavigation(articleUrl, {
                                parentUrl: pendingParentUrl,
                            });
                        }
                    }
                }
                if (event.data?.type === "zimantic-article-selection") {
                    const articleUrl = readModalUrl(event.data.url);
                    const activeArticleUrl = readModalUrl(activeModalUrl);
                    if (
                        !articleUrl ||
                        !activeArticleUrl ||
                        articleUrl.pathname !== activeArticleUrl.pathname
                    ) {
                        return;
                    }
                    syncModalStateUrl(articleUrl);
                    return;
                }
                if (event.data?.type === "zimantic-article-follow") {
                    const articleUrl = readModalUrl(event.data.url);
                    if (!articleUrl) return;
                    navigateArticleModal(articleUrl, "follow", {
                        parentUrl: readModalUrl(event.data.parent),
                    });
                }
                if (event.data?.type === "zimantic-article-navigate") {
                    const direction = event.data.direction;
                    if (direction !== "previous" && direction !== "next") return;
                    const articleUrl = readModalUrl(event.data.url);
                    navigateSearchResult(direction, articleUrl?.href || null);
                }
                if (event.data?.type === "zimantic-parent-article-navigate") {
                    const articleUrl = readModalUrl(event.data.url);
                    if (!articleUrl || !activeParentNavigation) return;
                    const target = articleUrlValue(articleUrl);
                    if (target !== activeParentNavigation.previous && target !== activeParentNavigation.next) {
                        return;
                    }
                    navigateArticleModal(articleUrl, "parent", { parentUrl: activeParentUrl });
                }
            });

            function setResultNode(node, item) {
                node.dataset.key = resultKey(item);
                const title = node.querySelector(".result-title");
                title.replaceChildren();
                let displayTitle = item.title || "(untitled)";
                if (item.url) {
                    const link = document.createElement("a");
                    link.href = item.url;
                    link.target = "_blank";
                    link.rel = "noopener";
                    if (item.mimetype) link.dataset.mimetype = item.mimetype;
                    link.textContent = displayTitle;
                    title.appendChild(link);
                } else {
                    title.textContent = displayTitle;
                }
                appendMediaBadge(title, item.mimetype);
                setSourceMeta(node, item);
                setResultUrl(node, item);
                const snippet = node.querySelector(".result-snippet");
                const lead = item.lead || "";
                if (lead) {
                    snippet.className = "result-snippet";
                    const blocks = previewBlocks(lead);
                    if (!previewEndsWithPunctuation(lead) && blocks.length) {
                        blocks[blocks.length - 1] += "…";
                    }
                    snippet.innerHTML = blocks
                        .map((block) => `<p>${highlight(block, current ? current.terms : [])}</p>`)
                        .join("");
                } else {
                    snippet.className = current?.streaming
                        ? "result-snippet loading"
                        : "result-snippet none";
                    snippet.textContent = current?.streaming
                        ? "Loading preview…"
                        : "No preview available for this result.";
                }
                setResultDebug(node, item);
                let thumb = node.querySelector(".result-thumb");
                if (item.url) {
                    if (!thumb) {
                        thumb = document.createElement("div");
                        thumb.className = "result-thumb";
                        node.appendChild(thumb);
                    }
                    thumb.dataset.url = item.url;
                } else if (thumb) {
                    thumb.remove();
                }
            }

            function newResultNode(item) {
                const node = document.createElement("li");
                node.className = "result";
                node.innerHTML = `<div class="result-body">
  <div class="result-title"></div>
  <div class="result-meta"></div>
  <div class="result-snippet"></div>
</div>`;
                setResultNode(node, item);
                return node;
            }

            function animateRemoval(node, animate = true) {
                if (!animate || reducedMotion() || !node.animate) {
                    node.remove();
                    return;
                }
                node.classList.add("result-exit");
                const animation = node.animate(
                    [
                        { opacity: 1, transform: "translateY(0)" },
                        { opacity: 0, transform: "translateY(-6px)" },
                    ],
                    { duration: 180, easing: "ease-in", fill: "forwards" },
                );
                animation.finished.then(
                    () => node.remove(),
                    () => node.remove(),
                );
            }

            function renderResults(items) {
                // During a stream, `source` events only change counts and a
                // snapshot whose page didn't move omits `results`, so the same
                // items array can arrive again. Skip the reconciliation in that
                // case; completion and paging always pass a fresh array and
                // still render (and load thumbnails).
                if (items === lastRenderedItems && current?.streaming) return;
                lastRenderedItems = items;
                if (!items.length) {
                    const oldList = resultList;
                    if (oldList) {
                        [...oldList.children].forEach((node) => animateRemoval(node, !current?.streaming));
                        resultList = null;
                        setTimeout(
                            () => {
                                if (!oldList.children.length) oldList.remove();
                            },
                            reducedMotion() ? 0 : 200,
                        );
                    }
                    $("results")
                        .querySelectorAll(".empty")
                        .forEach((node) => node.remove());
                    const empty = document.createElement("div");
                    empty.className = "empty";
                    empty.textContent = current?.streaming
                        ? "" // waiting for search results... but they often appear too fast to read this text so better to print nothing...
                        : "No results found in the available sources. Try using different words.";
                    $("results").appendChild(empty);
                    return;
                }
                $("results")
                    .querySelectorAll(".empty")
                    .forEach((node) => node.remove());
                if (!resultList) {
                    resultList = document.createElement("ol");
                    resultList.className = "results";
                    $("results").appendChild(resultList);
                }
                const existing = new Map([...resultList.children].map((node) => [node.dataset.key, node]));
                const before = new Map(
                    [...resultList.children].map((node) => [node.dataset.key, node.getBoundingClientRect()]),
                );
                const wanted = new Set(items.map(resultKey));
                const animateChanges = !current?.streaming;
                existing.forEach((node, key) => {
                    if (!wanted.has(key)) animateRemoval(node, animateChanges);
                });
                const added = [];
                items.forEach((item) => {
                    const key = resultKey(item);
                    const node = existing.get(key) || newResultNode(item);
                    setResultNode(node, item);
                    if (!existing.has(key)) added.push(node);
                    resultList.appendChild(node);
                });
                const after = new Map(
                    [...resultList.children].map((node) => [node.dataset.key, node.getBoundingClientRect()]),
                );
                if (!reducedMotion()) {
                    if (animateChanges) {
                        for (const [key, node] of [...resultList.children].map((node) => [
                            node.dataset.key,
                            node,
                        ])) {
                            const oldBox = before.get(key);
                            const newBox = after.get(key);
                            if (oldBox && newBox && node.animate) {
                                const dx = oldBox.left - newBox.left;
                                const dy = oldBox.top - newBox.top;
                                if (dx || dy) {
                                    node.animate(
                                        [
                                            { transform: `translate(${dx}px,${dy}px)` },
                                            { transform: "translate(0,0)" },
                                        ],
                                        { duration: 220, easing: "ease-out" },
                                    );
                                }
                            }
                        }
                    }
                    added.forEach((node) => {
                        node.classList.add("result-enter");
                        requestAnimationFrame(() => node.classList.remove("result-enter"));
                    });
                }
                // Intermediate snapshots only contain provisional ranking data. Defer
                // article reads for thumbnails until the final result set is available.
                if (!current?.streaming) loadThumbnails(items);
            }

            function pageRangeText(total, page) {
                if (!total) return "No results";
                const from = page * PAGE_SIZE + 1;
                const to = Math.min((page + 1) * PAGE_SIZE, total);
                return `${from}–${to} of ${total} result${total === 1 ? "" : "s"}`;
            }

            function syncPage(pageNumber) {
                // The server clamps out-of-range offsets and reports the
                // effective page, so the client never has to infer it from the
                // offset and its own page-size assumption.
                const page = Number(pageNumber);
                if (!current || !Number.isInteger(page) || page < 0) return;
                if (page === current.page) return;
                current.page = page;
                history.replaceState(
                    history.state,
                    "",
                    makeStateUrl(
                        current.question,
                        selectedSourceKeys,
                        page,
                        readLocationState().modal,
                    ),
                );
            }

            function searchStatusText() {
                if (!current) return "";
                const range = current.total
                    ? pageRangeText(current.total, current.page)
                    : current.sourcesTotal
                      ? "No results"
                      : "No searchable sources.";
                // Keep the source tally visible once the stream has finished, on
                // every page, so paging does not drop it from the status line.
                return current.sourcesTotal > 0 &&
                    current.completed === current.sourcesTotal
                    ? `${range} · ${current.completed}/${current.sourcesTotal} sources complete`
                    : range;
            }

            function sourceProgressText(completed, total) {
                const noun = total === 1 ? "source" : "sources";
                return `${completed}/${total} ${noun} complete`;
            }

            function announceSearchStatus(message = null) {
                const node = $("searchAnnouncement");
                if (!node) return;
                if (message) {
                    node.textContent = message;
                    return;
                }
                if (!current) {
                    node.textContent = "";
                    return;
                }
                // Before the stream's `started` event, 0/0 is not a result:
                // announcing "No searchable sources." here would be wrong.
                if (current.streaming && current.sourcesTotal === 0) {
                    node.textContent = "";
                    return;
                }
                if (current.sourcesTotal === 0) {
                    node.textContent = current.total
                        ? searchStatusText()
                        : current.streaming
                          ? ""
                          : "No sources are available to search.";
                    return;
                }
                node.textContent =
                    current.completed === current.sourcesTotal
                        ? searchStatusText()
                        : "";
            }

            function renderPager(total, pageCount) {
                const pager = $("pager");
                const focused = document.activeElement;
                const focusLabel =
                    focused instanceof HTMLElement && pager.contains(focused)
                        ? focused.textContent.trim()
                        : "";
                pager.innerHTML = "";
                if (total <= PAGE_SIZE && pageCount <= 1) {
                    if (focusLabel) {
                        const firstResult = $("results").querySelector(
                            ".result-title a[href]",
                        );
                        if (firstResult) firstResult.focus({ preventScroll: true });
                    }
                    return;
                }
                const pageLink = (text, page, disabled) => {
                    const link = document.createElement(disabled ? "span" : "a");
                    link.textContent = text;
                    if (disabled) {
                        link.className = "disabled";
                        link.setAttribute("aria-disabled", "true");
                    } else {
                        // A real href keeps middle-click/open-in-new-tab working; the click
                        // handler below intercepts it to avoid a full document reload.
                        link.href = makePageUrl(page);
                        link.dataset.page = String(page);
                    }
                    return link;
                };
                const prev = pageLink("← Previous", current.page - 1, current.page === 0);
                const next = pageLink("Next →", current.page + 1, current.page >= pageCount - 1);
                const label = document.createElement("span");
                const from = total ? current.page * PAGE_SIZE + 1 : 0;
                const to = Math.min((current.page + 1) * PAGE_SIZE, total);
                label.textContent = `${from}–${to} of ${total}`;
                pager.append(prev, label, next);
                if (current.page > 0) {
                    // Shortcut back to the first page from anywhere in the pager.
                    pager.prepend(pageLink("« First", 0, false));
                }
                if (focusLabel) {
                    const links = [...pager.querySelectorAll("a[data-page]")];
                    const target =
                        links.find((link) => link.textContent.trim() === focusLabel) ||
                        (focusLabel.includes("Next")
                            ? links[links.length - 1]
                            : links[0]);
                    if (target) target.focus({ preventScroll: true });
                }
            }

            function renderView() {
                if (!current) return;
                // The server already sliced this page and clamped any
                // out-of-range offset; there is nothing to re-slice here.
                const pageCount = Math.max(1, Math.ceil(current.total / PAGE_SIZE));
                renderSourceFilters();
                renderFilterLine(current.total);
                renderPager(current.total, pageCount);
                renderResults(current.pageItems);
                renderMatchedBy();
                const articleUrl = readModalUrl(activeModalUrl);
                if (
                    articleUrl &&
                    (!activeSearchResult ||
                        activeSearchResult.page !== current.page ||
                        activeSearchResult.url !== articleUrlValue(articleUrl))
                ) {
                    rememberSearchResultUrl(articleUrl.href);
                }
                postArticleModalNavigation();
            }

            function renderNotice() {
                if (!current) {
                    $("notice").textContent = "";
                    return;
                }
                const outcomes = [...current.outcomes.values()];
                // Only a hard "error" status counts as a failure; a "partial"
                // source still returned results, so it must not trip this banner.
                const errors = outcomes.filter((outcome) => outcome.status === "error");
                $("notice").innerHTML =
                    current.completed === current.sourcesTotal &&
                    outcomes.length > 0 &&
                    errors.length === outcomes.length
                        ? "<div class='notice'><strong>All sources failed.</strong> Check your sources and the Kiwix service, then reload or restart the server.</div>"
                        : "";
            }

            function cancelThumbnails() {
                thumbnailGeneration++;
                thumbnailQueue.length = 0;
                thumbnailControllers.forEach((controller) => controller.abort());
                thumbnailControllers.clear();
            }

            function pumpThumbnails() {
                while (activeThumbnails < THUMB_CONCURRENCY && thumbnailQueue.length) {
                    const task = thumbnailQueue.shift();
                    activeThumbnails++;
                    fetchThumbnail(task.url)
                        .then(task.resolve, () => task.resolve(null))
                        .finally(() => {
                            activeThumbnails--;
                            pumpThumbnails();
                        });
                }
            }

            async function readStart(response, maxBytes) {
                // Both paths cap at maxBytes *bytes* of UTF-8, so a page with
                // multibyte characters yields the same amount of text either way.
                if (!response.body || !response.body.getReader) {
                    const buffer = new Uint8Array(await response.arrayBuffer());
                    return new TextDecoder().decode(buffer.subarray(0, maxBytes));
                }
                const reader = response.body.getReader();
                const decoder = new TextDecoder();
                let text = "";
                let bytes = 0;
                try {
                    while (bytes < maxBytes) {
                        const part = await reader.read();
                        if (part.done) break;
                        // Stop before a chunk that would cross the cap.
                        if (bytes + part.value.byteLength > maxBytes) break;
                        bytes += part.value.byteLength;
                        text += decoder.decode(part.value, { stream: true });
                    }
                } finally {
                    reader.cancel().catch(() => {});
                }
                return text;
            }

            function pickThumbnail(html, articleUrl) {
                const doc = new DOMParser().parseFromString(html, "text/html");
                for (const image of doc.querySelectorAll("img")) {
                    const src = (image.getAttribute("src") || "").trim();
                    if (!src || src.startsWith("data:") || /\.svg(?:[?#]|$)/i.test(src)) continue;
                    if (/(^|[\W_])(icon|logo|avatar|symbol|sprite|spacer|blank)/i.test(src)) continue;
                    if (/\/users?\/profiles?\//i.test(src)) continue;
                    try {
                        return new URL(src, articleUrl).href;
                    } catch (_) {}
                }
                return null;
            }

            async function fetchThumbnail(url) {
                // item.url points at the /content/ shell, which has no article media;
                // pull the raw article so the thumbnail picks the real image.
                const rawUrl = rawArticleUrl(url);
                const requestUrl = rawUrl ? rawUrl.href : url;
                const controller = new AbortController();
                thumbnailControllers.add(controller);
                try {
                    const response = await fetch(requestUrl, { signal: controller.signal });
                    if (!response.ok) return null;
                    return pickThumbnail(await readStart(response, THUMB_LIMIT), requestUrl);
                } catch (_) {
                    return null;
                } finally {
                    thumbnailControllers.delete(controller);
                }
            }

            function thumbnailFor(url) {
                if (!thumbnailCache.has(url)) {
                    thumbnailCache.set(
                        url,
                        new Promise((resolve) => {
                            thumbnailQueue.push({ url, resolve });
                            pumpThumbnails();
                        }),
                    );
                }
                return thumbnailCache.get(url);
            }

            function loadThumbnails(items) {
                const generation = thumbnailGeneration;
                items.forEach((item) => {
                    if (!item.url) return;
                    thumbnailFor(item.url).then((src) => {
                        if (generation !== thumbnailGeneration) return;
                        const node =
                            resultList &&
                            [...resultList.children].find((child) => child.dataset.key === resultKey(item));
                        const slot = node && node.querySelector(".result-thumb");
                        if (!slot || slot.firstChild) return;
                        if (!src) {
                            slot.remove();
                            return;
                        }
                        const image = document.createElement("img");
                        image.alt = "";
                        image.loading = "lazy";
                        image.decoding = "async";
                        image.src = src;
                        image.addEventListener("error", () => slot.remove());
                        slot.appendChild(image);
                    });
                });
            }

            function waitForRetry(delay, signal) {
                return new Promise((resolve, reject) => {
                    const timer = setTimeout(() => {
                        signal.removeEventListener("abort", abort);
                        resolve();
                    }, delay);
                    const abort = () => {
                        clearTimeout(timer);
                        signal.removeEventListener("abort", abort);
                        const error = new Error("aborted");
                        error.name = "AbortError";
                        reject(error);
                    };
                    signal.addEventListener("abort", abort, { once: true });
                    if (signal.aborted) abort();
                });
            }

            async function consumeStream(response, sequence) {
                if (!response.body) throw new Error("streaming response has no body");
                const reader = response.body.getReader();
                const decoder = new TextDecoder();
                let buffer = "";
                let busy = false;
                const applyPage = (event) => {
                    current.completed = event.completed;
                    // A snapshot whose page didn't move omits `results`; keep the
                    // page we already have instead of clearing it.
                    if (Array.isArray(event.results)) current.pageItems = event.results;
                    current.total = event.total || 0;
                    syncPage(event.page);
                    if (event.counts) current.counts = event.counts;
                    if (event.matched_by) current.matchedBy = event.matched_by;
                };
                const consumeLine = (line) => {
                    if (!line.trim() || sequence !== searchSequence) return;
                    let event;
                    try {
                        event = JSON.parse(line);
                    } catch (_) {
                        // A truncated line from a dropped stream is ignored; the
                        // caller degrades to the last snapshot instead of failing.
                        return;
                    }
                    if (event.type === "started") {
                        current.sourcesTotal = event.total_sources || 0;
                        current.completed = 0;
                        if (Array.isArray(event.sources)) {
                            current.searchKeys = event.sources
                                .map((source) => source?.key)
                                .filter(Boolean);
                            // A hidden version alias (for example, a bookmarked
                            // mini/nopic variant) resolves to the chosen book.
                            // Reflect that canonical key in the filter UI.
                            if (event.all_sources === false && current.searchKeys.length) {
                                setSelectedSources(current.searchKeys);
                            }
                        }
                        if (typeof event.all_sources === "boolean") {
                            current.searchAll = event.all_sources;
                        }
                        $("status").textContent =
                            `Searching ${current.sourcesTotal} source${current.sourcesTotal === 1 ? "" : "s"}…`;
                    } else if (event.type === "source") {
                        current.completed = event.completed;
                        current.outcomes.set(event.source.key, event);
                        if (typeof event.count === "number") current.counts[event.source.key] = event.count;
                        $("status").textContent = sourceProgressText(
                            event.completed,
                            event.total_sources,
                        );
                        scheduleStreamRender(sequence);
                    } else if (event.type === "progress") {
                        // A slow source has not finished yet. Keep the status
                        // clock moving without re-rendering the result list.
                        current.completed = event.completed;
                        if (typeof event.total_sources === "number") {
                            current.sourcesTotal = event.total_sources;
                        }
                        $("status").textContent =
                            `Searching… ${Number(event.elapsed || 0).toFixed(1)}s · ` +
                            sourceProgressText(event.completed, event.total_sources);
                    } else if (event.type === "snapshot") {
                        applyPage(event);
                        scheduleStreamRender(sequence);
                        return true;
                    } else if (event.type === "done") {
                        applyPage(event);
                        current.streaming = false;
                        cancelStreamRender();
                        renderView();
                        $("status").textContent = searchStatusText();
                        announceSearchStatus();
                        renderNotice();
                    } else if (event.type === "error") {
                        busy = Boolean(event.busy);
                        $("status").textContent = event.error || "Search failed";
                        announceSearchStatus($("status").textContent);
                    }
                    return false;
                };
                while (true) {
                    const part = await reader.read();
                    if (part.done) break;
                    buffer += decoder.decode(part.value, { stream: true });
                    const lines = buffer.split("\n");
                    buffer = lines.pop() || "";
                    for (const line of lines) {
                        if (consumeLine(line)) await yieldToBrowser();
                    }
                }
                if (buffer && consumeLine(buffer)) await yieldToBrowser();
                if (busy) return true;
                if (sequence === searchSequence && current && current.streaming) {
                    current.streaming = false;
                    cancelStreamRender();
                    renderView();
                }
                return false;
            }

            async function runSearch(question, selectedSources = [], page = 0, scopeSources) {
                question = question.trim();
                if (!question) return;
                sourceFilterShowAll = false;
                sourceFilterShowOther = false;
                if (requestController) requestController.abort();
                if (pageController) pageController.abort();
                cancelStreamRender();
                // Start each search's slow-render cadence from now, so the first
                // results paint one interval in rather than immediately after the
                // previous search's last render.
                lastStreamRenderAt = performance.now();
                cancelThumbnails();
                thumbnailCache.clear();
                const controller = new AbortController();
                requestController = controller;
                const sequence = ++searchSequence;
                const pageIndex = Number.isInteger(page) && page >= 0 ? page : 0;
                activeSearchResult = null;
                const requested = [...new Set(selectionValues(selectedSources))];
                const scopeKeys = [
                    ...new Set(
                        selectionValues(
                            scopeSources === undefined ? requested : scopeSources,
                        ),
                    ),
                ];
                setSelectedSources(requested);
                current = {
                    question,
                    terms: queryTerms(question),
                    outcomes: new Map(),
                    counts: {},
                    pageItems: [],
                    matchedBy: null,
                    total: 0,
                    completed: 0,
                    sourcesTotal: 0,
                    streaming: true,
                    page: pageIndex,
                    searchKeys: scopeKeys,
                    // The server decides whether this selection is small
                    // enough to search on its own; `started` replaces both
                    // values with the scope it actually searched.
                    searchAll: scopeKeys.length === 0,
                };
                announceSearchStatus();
                document.body.classList.add("search-active");
                window.scrollTo({ top: 0, left: 0, behavior: "auto" });
                $("searchSummary").hidden = false;
                $("searchButton").disabled = true;
                $("status").innerHTML =
                    `Searching for <span class="query-used">"${escapeHtml(question)}"</span>…`;
                $("notice").textContent = "";
                $("pager").innerHTML = "";
                renderResults([]);
                try {
                    const selected = activeSources();
                    if (!selected.length) {
                        $("status").textContent = "No sources are available to search.";
                        announceSearchStatus($("status").textContent);
                        return;
                    }
                    const params = new URLSearchParams({
                        q: question,
                        limit: String(PAGE_SIZE),
                        offset: String(pageIndex * PAGE_SIZE),
                    });
                    sourceFilterParams(params);
                    searchScopeParams(params, scopeKeys);
                    // The server promotes large selections to one full search, so
                    // this request's scope is reported in the `started` event. Page
                    // size, offset and the display filter are not cached keys.
                    let busyRetries = 0;
                    while (true) {
                        const streamController = new AbortController();
                        const abortStream = () => streamController.abort();
                        const streamTimeout = setTimeout(
                            () => streamController.abort(),
                            STREAM_SEARCH_TIMEOUT,
                        );
                        controller.signal.addEventListener("abort", abortStream, { once: true });
                        let busy;
                        try {
                            const response = await fetch(appPath("/search/stream") + "?" + params, {
                                signal: streamController.signal,
                            });
                            if (!response.ok) throw new Error(`HTTP ${response.status}`);
                            busy = await consumeStream(response, sequence);
                        } finally {
                            clearTimeout(streamTimeout);
                            controller.signal.removeEventListener("abort", abortStream);
                        }
                        if (!busy || busyRetries >= BUSY_RETRY_LIMIT || sequence !== searchSequence) break;
                        busyRetries++;
                        const delay = Math.min(
                            BUSY_RETRY_BASE_DELAY * 2 ** (busyRetries - 1),
                            BUSY_RETRY_MAX_DELAY,
                        );
                        $("status").textContent = `Server is busy; retrying in ${delay / 1000}s…`;
                        await waitForRetry(delay, controller.signal);
                    }
                } catch (error) {
                    if (
                        error.name === "AbortError" &&
                        !controller.signal.aborted &&
                        sequence === searchSequence
                    ) {
                        // Some mobile browsers and proxies do not expose a timely
                        // readable body for chunked NDJSON. The JSON endpoint avoids
                        // that transport without changing the search result.
                        current.streaming = false;
                        await loadPage(pageIndex);
                    } else if (error.name !== "AbortError" && sequence === searchSequence) {
                        $("status").textContent = `Search failed: ${error.message}`;
                        announceSearchStatus($("status").textContent);
                    }
                } finally {
                    if (sequence === searchSequence) {
                        current.streaming = false;
                        cancelStreamRender();
                        $("searchButton").disabled = false;
                        requestController = null;
                    }
                }
            }

            async function loadPage(page) {
                // Paging and display filtering reuse the server's cached pool for
                // the current search scope and return a single page. No document
                // reload, no re-stream.
                if (!current) return;
                const sequence = searchSequence;
                const pageIndex = Number.isInteger(page) && page >= 0 ? page : 0;
                if (pageController) pageController.abort();
                // Paging takes over the view: stop any in-flight stream so its
                // final `done` event cannot overwrite this page's results.
                if (requestController) {
                    requestController.abort();
                    requestController = null;
                }
                const controller = new AbortController();
                pageController = controller;
                const params = new URLSearchParams({
                    q: current.question,
                    limit: String(PAGE_SIZE),
                    offset: String(pageIndex * PAGE_SIZE),
                });
                sourceFilterParams(params);
                searchScopeParams(params, current.searchAll ? [] : current.searchKeys || []);
                $("status").textContent = "Loading page…";
                try {
                    const response = await fetch(appPath("/search") + "?" + params, {
                        signal: controller.signal,
                    });
                    if (!response.ok) throw new Error(`HTTP ${response.status}`);
                    const items = await response.json();
                    if (sequence !== searchSequence || controller !== pageController) return;
                    current.page = pageIndex;
                    current.pageItems = items;
                    current.total = Number(response.headers.get("X-Total-Count") || items.length);
                    syncPage(response.headers.get("X-Page"));
                    if (debugMode) {
                        const matchedBy = response.headers.get("X-Matched-By");
                        if (matchedBy) {
                            try {
                                current.matchedBy = JSON.parse(matchedBy);
                            } catch (_) {
                                current.matchedBy = null;
                            }
                        }
                    }
                    renderView();
                    $("status").textContent = searchStatusText();
                    announceSearchStatus();
                    window.scrollTo({ top: 0, left: 0, behavior: "auto" });
                } catch (error) {
                    if (
                        error.name !== "AbortError" &&
                        sequence === searchSequence &&
                        controller === pageController
                    ) {
                        $("status").textContent = `Search failed: ${error.message}`;
                        announceSearchStatus($("status").textContent);
                    }
                } finally {
                    if (controller === pageController) pageController = null;
                }
            }

            function applySourceSelection(question, selectedSources, page = 0, scopeSources) {
                // The running search already knows its real scope. A full pool
                // can serve any selected source that it actually searched, so
                // narrowing is display-only. A source outside that pool (for
                // example a remote source beyond max_remote_sources) must be
                // searched explicitly instead of filtering to an empty page.
                const requested = [...new Set(selectionValues(selectedSources))];
                if (!requested.length) {
                    if (current && current.question === question && current.searchAll) {
                        setSelectedSources(requested);
                        loadPage(page);
                        return;
                    }
                    runSearch(question, requested, page);
                    return;
                }
                if (current && current.question === question) {
                    const scope = new Set(current.searchKeys || []);
                    if (requested.every((key) => scope.has(key))) {
                        setSelectedSources(requested);
                        loadPage(page);
                        return;
                    }
                }
                runSearch(
                    question,
                    requested,
                    page,
                    scopeSources === undefined ? requested : scopeSources,
                );
            }

            function makeStateUrl(
                question,
                selectedSources,
                page,
                modal = null,
                scopeSources,
            ) {
                // Keep search and modal state in the query string so links are shareable
                // and Back/Forward just work.
                const url = new URL(location.href);
                url.hash = "";
                url.search = "";
                if (question) url.searchParams.set("q", question);
                const sourceValues =
                    selectedSources instanceof Set
                        ? [...selectedSources]
                        : Array.isArray(selectedSources)
                          ? selectedSources
                          : selectedSources
                            ? [selectedSources]
                            : [];
                const displayValues = [...new Set(selectionValues(sourceValues))];
                const explicitScope = scopeSources !== undefined;
                const sameSearch =
                    explicitScope || (current && current.question === question);
                const scopeValues = explicitScope
                    ? [...new Set(selectionValues(scopeSources))]
                    : sameSearch
                      ? [...new Set(current.searchKeys || [])]
                      : displayValues;
                const fullSearch =
                    sameSearch &&
                    (explicitScope
                        ? scopeValues.length === 0
                        : Boolean(current.searchAll));
                if (fullSearch) {
                    // A full-search pool is already cached; these keys only
                    // narrow the displayed results and must not become a new
                    // `zim` search scope when the URL is reloaded.
                    displayValues.forEach((source) =>
                        url.searchParams.append("source", source),
                    );
                } else {
                    scopeValues.forEach((source) =>
                        url.searchParams.append("zim", source),
                    );
                    if (sameSearch) {
                        const scopeSet = new Set(scopeValues);
                        const displaySet = new Set(displayValues);
                        const differs =
                            scopeSet.size !== displaySet.size ||
                            [...displaySet].some((key) => !scopeSet.has(key));
                        if (differs) {
                            displayValues.forEach((source) =>
                                url.searchParams.append("source", source),
                            );
                        }
                    }
                }
                if (page > 0) url.searchParams.set("page", String(page + 1));
                if (modal) url.searchParams.set("modal", modal);
                // Search state owns q/source/zim/page/modal, but a library view
                // also has its filter in the URL. Keep it when opening an
                // article or another history entry from /library.
                if (
                    typeof appPath === "function" &&
                    location.pathname === appPath("/library")
                ) {
                    const filter =
                        ($("libraryFilter")?.value || "").trim() ||
                        readLibraryFilter();
                    if (filter) url.searchParams.set("filter", filter);
                }
                return url.pathname + url.search;
            }

            function makePageUrl(page) {
                return makeStateUrl(current.question, selectedSourceKeys, page);
            }

            function syncModalStateUrl(articleUrl) {
                // Keep the address bar pointing at the article that is actually
                // visible, including when a link inside the article navigated the
                // shell or when a text selection was shared.
                const modal = $("articleModal");
                if (!modal || modal.hidden) return;
                const value = articleUrl ? articleUrlValue(articleUrl) : null;
                const url = new URL(location.href);
                if (value) {
                    if (url.searchParams.get("modal") === value) return;
                    url.searchParams.set("modal", value);
                } else {
                    if (!url.searchParams.has("modal")) return;
                    url.searchParams.delete("modal");
                }
                history.replaceState(history.state, "", url.pathname + url.search + url.hash);
            }

            function normalizePageParam() {
                // Direct links can carry page=0, page=abc or a non-canonical
                // spelling. Keep the value the app actually uses, and remove
                // the first-page parameter instead of leaving a stale value in
                // the address bar.
                const params = new URLSearchParams(location.search);
                if (!params.has("page")) return;
                const raw = params.get("page");
                const page = Number.parseInt(raw || "", 10);
                const canonical =
                    Number.isInteger(page) && page >= 1 ? String(page) : null;
                if (raw === canonical && canonical !== "1") return;
                if (canonical && canonical !== "1") {
                    params.set("page", canonical);
                } else {
                    params.delete("page");
                }
                const url = new URL(location.href);
                url.search = params.toString();
                history.replaceState(
                    history.state,
                    "",
                    url.pathname + url.search + url.hash,
                );
            }

            function readLocationState() {
                const params = new URLSearchParams(location.search);
                const pageNumber = Number.parseInt(params.get("page") || "1", 10);
                const scopeSources = [
                    ...new Set(
                        [
                            ...params.getAll("zim"),
                            ...params.getAll("src"),
                        ].filter(Boolean),
                    ),
                ];
                const displaySources = [
                    ...new Set(params.getAll("source").filter(Boolean)),
                ];
                return {
                    question: (params.get("q") || "").trim().slice(0, MAX_QUERY_LENGTH),
                    // `zim` is the canonical search-scope parameter and
                    // `source` is the display filter for a full-search pool.
                    // Keep reading legacy `src` links too.
                    selectedSources: displaySources.length
                        ? displaySources
                        : scopeSources,
                    scopeSources,
                    hasScope: params.has("zim") || params.has("src"),
                    page: Number.isInteger(pageNumber) && pageNumber >= 1 ? pageNumber - 1 : 0,
                    modal: params.get("modal") || null,
                };
            }

            function syncArticleModal(modalValue) {
                const articleUrl = readModalUrl(modalValue);
                if (articleUrl) {
                    // A shared /library?modal=... link has no history.state, so infer
                    // the library navigation context from the page itself.
                    const navigationKind =
                        history.state?.zimanticModalNavigation ||
                        (location.pathname === appPath("/library") ? "library" : "search");
                    rememberArticleAlias(
                        articleUrl,
                        readModalUrl(history.state?.zimanticLinkedArticle),
                    );
                    showArticleModal(
                        articleUrl.href,
                        navigationKind,
                        readModalUrl(history.state?.zimanticParent),
                    );
                } else {
                    closeArticleModal({ navigateHistory: false });
                }
            }

            $("sourceFilterSearch").addEventListener("input", () => {
                sourceFilterShowAll = false;
                sourceFilterShowOther = false;
                renderSourceFilters();
            });

            $("sourceFilterOptions").addEventListener("click", (event) => {
                const button = event.target.closest("button[data-source], button[data-source-action]");
                if (!button || button.disabled) return;
                const key = button.dataset.source || null;
                if (!current) {
                    if (button.dataset.sourceAction === "all") {
                        selectedSourceKeys.clear();
                    } else if (key && selectedSourceKeys.has(key)) {
                        selectedSourceKeys.delete(key);
                    } else if (key) {
                        selectedSourceKeys.add(key);
                    }
                    const selected = [...selectedSourceKeys];
                    history.replaceState(
                        history.state,
                        "",
                        makeStateUrl("", selected, 0),
                    );
                    renderSourceSelectionSummary();
                    const buttons = [
                        ...$("sourceFilterOptions").querySelectorAll("button"),
                    ];
                    const next =
                        (key && buttons.find((item) => item.dataset.source === key)) ||
                        buttons.find(
                            (item) => item.dataset.sourceAction === "all",
                        ) ||
                        buttons[0];
                    if (next) next.focus();
                    else $("question").focus();
                    return;
                }
                if (button.dataset.sourceAction === "all") {
                    selectedSourceKeys.clear();
                } else if (button.dataset.sourceAction === "more") {
                    // Reveal the remaining matching source chips without
                    // changing the search scope or the current selection.
                    const hidden = sourceFilterLayout().otherOutcomes.map(
                        (outcome) => outcome.source.key,
                    );
                    sourceFilterShowAll = true;
                    renderSourceFilters();
                    const next = [
                        ...$("sourceFilterOptions").querySelectorAll("button"),
                    ].find(
                        (item) =>
                            hidden.length && item.dataset.source === hidden[0],
                    );
                    if (next) next.focus({ preventScroll: true });
                    return;
                } else if (button.dataset.sourceAction === "other") {
                    // "Other" is a disclosure, not a filter. Reveal the group's
                    // individual source chips so the reader can pick sources
                    // deliberately instead of silently selecting all of them.
                    const hidden = sourceFilterLayout().otherOutcomes.map(
                        (outcome) => outcome.source,
                    );
                    sourceFilterShowOther = true;
                    renderSourceFilters();
                    const next = hidden.length
                        ? [
                              ...$("sourceFilterOptions").querySelectorAll("button"),
                          ].find((item) => item.dataset.source === hidden[0].key)
                        : null;
                    if (next) next.focus({ preventScroll: true });
                    return;
                } else if (selectedSourceKeys.has(key)) {
                    selectedSourceKeys.delete(key);
                } else {
                    selectedSourceKeys.add(key);
                }
                const selected = [...selectedSourceKeys];
                commitAutoSearchHistory();
                const scopeKeys = [...new Set(current.searchKeys || [])];
                const addsOutside = selected.some(
                    (source) => !scopeKeys.includes(source),
                );
                const nextScope = !selected.length
                    ? []
                    : current.searchAll
                      ? addsOutside
                        ? selected
                        : []
                      : addsOutside
                        ? selected
                        : scopeKeys;
                history.pushState(
                    null,
                    "",
                    makeStateUrl(current.question, selected, 0, null, nextScope),
                );
                renderSourceFilters();
                applySourceSelection(current.question, selected, 0, nextScope);
            });

            $("results").addEventListener("click", (event) => {
                const target = event.target instanceof Element ? event.target : null;
                const link = target?.closest(".result-title a[href]");
                if (!link || modifiedClick(event)) return;
                if (new URL(link.href, location.href).origin !== location.origin) return;
                if (!isHtmlMimetype(link.dataset.mimetype)) return;
                event.preventDefault();
                openArticleModal(link);
            });

            $("libraryGrid").addEventListener("click", (event) => {
                const target = event.target instanceof Element ? event.target : null;
                const link = target?.closest("a[data-library-article]");
                if (!link || modifiedClick(event)) return;
                if (new URL(link.href, location.href).origin !== location.origin) return;
                event.preventDefault();
                navigateArticleModal(new URL(link.href, location.href), "library");
            });
            $("libraryInfoModal").addEventListener("click", (event) => {
                const target = event.target instanceof Element ? event.target : null;
                if (target?.classList.contains("library-info-backdrop")) {
                    closeLibraryInfo();
                    return;
                }
                const link = target?.closest("a[data-library-article]");
                if (!link || modifiedClick(event)) return;
                if (new URL(link.href, location.href).origin !== location.origin) return;
                event.preventDefault();
                // The article modal adds its own history entry; keep the
                // details marker so Back can return to the details view.
                closeLibraryInfo({ navigateHistory: false });
                navigateArticleModal(new URL(link.href, location.href), "library");
            });
            $("libraryInfoClose").addEventListener("click", closeLibraryInfo);
            $("libraryInfoModal").addEventListener("keydown", (event) => {
                if (event.key !== "Tab" || $("libraryInfoModal").hidden) return;
                const modal = $("libraryInfoModal");
                const focusable = [
                    ...modal.querySelectorAll(
                        'a[href], button:not([disabled]):not([tabindex="-1"]), ' +
                            "input:not([disabled]), select:not([disabled]), " +
                            "textarea:not([disabled]), summary, " +
                            '[tabindex]:not([tabindex="-1"])',
                    ),
                ].filter((node) => node.getClientRects().length > 0);
                if (!focusable.length) return;
                const first = focusable[0];
                const last = focusable[focusable.length - 1];
                if (event.shiftKey && document.activeElement === first) {
                    event.preventDefault();
                    last.focus();
                } else if (!event.shiftKey && document.activeElement === last) {
                    event.preventDefault();
                    first.focus();
                }
            });
            $("libraryFilter").addEventListener("input", () => {
                updateLibraryFilterUrl();
                renderLibrary();
            });
            $("libraryShuffle").addEventListener("click", shuffleLibrary);

            $("pager").addEventListener("click", (event) => {
                const link = event.target.closest("a[data-page]");
                if (!link || !current) return;
                event.preventDefault();
                const page = Number(link.dataset.page);
                commitAutoSearchHistory();
                history.pushState(null, "", makeStateUrl(current.question, selectedSourceKeys, page));
                loadPage(page);
            });

            $("articleModal").addEventListener("click", (event) => {
                if (event.target.classList.contains("article-modal-backdrop")) {
                    closeArticleModal();
                }
            });
            $("externalLinkBack").addEventListener("click", hideExternalLinkNotice);
            $("externalLinkNotice").addEventListener("click", (event) => {
                if (event.target === event.currentTarget) hideExternalLinkNotice();
            });
            $("externalLinkNotice").addEventListener("keydown", (event) => {
                if (event.key !== "Tab" || $("externalLinkNotice").hidden) return;
                const notice = $("externalLinkNotice");
                const focusable = [
                    ...notice.querySelectorAll(
                        'a[href], button:not([disabled]):not([tabindex="-1"]), ' +
                            "input:not([disabled]), select:not([disabled]), " +
                            "textarea:not([disabled]), summary, " +
                            '[tabindex]:not([tabindex="-1"])',
                    ),
                ].filter((node) => node.getClientRects().length > 0);
                if (!focusable.length) return;
                const first = focusable[0];
                const last = focusable[focusable.length - 1];
                if (!notice.contains(document.activeElement)) {
                    event.preventDefault();
                    first.focus();
                } else if (
                    event.shiftKey &&
                    (document.activeElement === first ||
                        document.activeElement === notice)
                ) {
                    event.preventDefault();
                    last.focus();
                } else if (!event.shiftKey && document.activeElement === last) {
                    event.preventDefault();
                    first.focus();
                }
            });
            // The article modal is an iframe, so Tab can move focus out of the
            // frame to the parent document. The page behind the modal is inert,
            // but the body itself can still receive focus; send it back to the
            // article instead of leaving the dialog.
            document.addEventListener("focusin", (event) => {
                const modal = $("articleModal");
                if (!modal || modal.hidden || modal.contains(event.target)) return;
                const frame = $("articleModalFrame");
                if (frame) frame.focus();
            });

            window.addEventListener("keydown", (event) => {
                if (event.key === "Escape" && !$("externalLinkNotice").hidden) {
                    event.preventDefault();
                    hideExternalLinkNotice();
                    return;
                }
                if (event.key === "Escape" && !$("libraryInfoModal").hidden) {
                    event.preventDefault();
                    closeLibraryInfo();
                    return;
                }
                if (event.key === "Escape" && !$("articleModal").hidden) {
                    event.preventDefault();
                    closeArticleModal();
                }
            });

            function resetResults() {
                // Clear everything a search painted, without touching history. Removes
                // search-active so the empty page re-centers like a fresh load.
                sourceFilterShowAll = false;
                sourceFilterShowOther = false;
                clearTimeout(autoSubmitTimer);
                autoSubmitTimer = null;
                cancelAutoSearchHistory();
                if (requestController) requestController.abort();
                requestController = null;
                if (pageController) pageController.abort();
                pageController = null;
                searchSequence++; // invalidate any in-flight stream callbacks
                cancelStreamRender();
                cancelThumbnails();
                thumbnailCache.clear();
                current = null;
                activeSearchResult = null;
                selectedSourceKeys = new Set();
                resultList = null;
                announceSearchStatus();
                document.body.classList.remove("search-active");
                $("searchSummary").hidden = true;
                $("sourceFilterToolbar").hidden = true;
                $("status").textContent = "";
                renderMatchedBy();
                $("sourceFilters").hidden = true;
                $("sourceFilterOptions").replaceChildren();
                $("sourceFilterStatus").textContent = "";
                $("sourceFilterSearch").value = "";
                $("filterLine").innerHTML = "";
                $("notice").textContent = "";
                $("pager").innerHTML = "";
                const empty = document.createElement("div");
                empty.className = "empty";
                $("results").replaceChildren(empty);
                $("searchButton").disabled = false;
            }

            function clearSearch(pushHistory) {
                // Explicitly submitted with an empty field: drop the prior search and
                // re-center. A cleared-but-not-submitted field keeps its results, so an
                // empty debounce never wipes the page on its own.
                const hadSearch = current !== null || Boolean(location.search);
                resetResults();
                $("question").value = "";
                if (hadSearch) {
                    history[pushHistory ? "pushState" : "replaceState"](null, "", location.pathname);
                }
                $("question").focus();
                window.scrollTo({ top: 0, left: 0, behavior: "auto" });
            }

            function toggleHeadingSearch() {
                if (location.pathname === appPath("/library")) {
                    history.pushState(null, "", appPath("/"));
                    initialize();
                    return;
                }
                if (current) {
                    previousHeadingSearch = {
                        question: current.question,
                        selectedSources: [...selectedSourceKeys],
                        scopeSources: current.searchAll
                            ? []
                            : [...new Set(current.searchKeys || [])],
                        page: current.page,
                    };
                    clearSearch(true);
                    return;
                }
                if (!previousHeadingSearch) return;
                const previous = previousHeadingSearch;
                $("question").value = previous.question;
                commitAutoSearchHistory();
                history.pushState(
                    null,
                    "",
                    makeStateUrl(
                        previous.question,
                        previous.selectedSources,
                        previous.page,
                        null,
                        previous.scopeSources,
                    ),
                );
                runSearch(
                    previous.question,
                    previous.selectedSources,
                    previous.page,
                    previous.scopeSources,
                );
            }

            function celebrateHeadingClick(event) {
                if (reducedMotion()) return;
                const heading = $("homeHeading");
                if (!heading) return;

                const burst = document.createElement("div");
                burst.className = "confetti-burst";
                burst.setAttribute("aria-hidden", "true");
                document.body.appendChild(burst);

                const rect = heading.getBoundingClientRect();
                const centerX = rect.left + rect.width / 2;
                const centerY = rect.top + rect.height / 2;
                const hasPointerPosition =
                    Number.isFinite(event?.clientX) &&
                    Number.isFinite(event?.clientY) &&
                    (event.clientX !== 0 || event.clientY !== 0);
                const originX = hasPointerPosition ? event.clientX : centerX;
                const originY = hasPointerPosition ? event.clientY : centerY;
                const offsetX = originX - centerX;
                const offsetY = originY - centerY;
                const offsetLength = Math.hypot(offsetX, offsetY);
                const clickTimePhase = ((Date.now() % 1000) / 1000) * Math.PI * 2;
                const direction = offsetLength > 4 ? Math.atan2(offsetY, offsetX) : clickTimePhase;
                const colors = ["var(--accent)", "var(--ok)", "var(--err)", "var(--text)"];
                const pieceCount = 28;
                let remaining = pieceCount;
                for (let index = 0; index < pieceCount; index++) {
                    const piece = document.createElement("span");
                    piece.className = "confetti-piece";
                    piece.style.left = `${originX}px`;
                    piece.style.top = `${originY}px`;
                    piece.style.setProperty(
                        "--confetti-color",
                        colors[Math.floor(Math.random() * colors.length)],
                    );
                    const angle = direction + (Math.random() - 0.5) * (Math.PI / 2.5);
                    const distance = 100 + Math.random() * 140;
                    piece.style.setProperty("--dx", `${Math.cos(angle) * distance}px`);
                    piece.style.setProperty("--dy", `${Math.sin(angle) * distance}px`);
                    piece.style.setProperty("--start-rotation", `${Math.random() * 180}deg`);
                    piece.style.setProperty("--end-rotation", `${360 + Math.random() * 720}deg`);
                    piece.style.width = `${4 + Math.random() * 5}px`;
                    piece.style.height = `${8 + Math.random() * 7}px`;
                    piece.addEventListener(
                        "animationend",
                        () => {
                            piece.remove();
                            remaining--;
                            if (!remaining) burst.remove();
                        },
                        { once: true },
                    );
                    burst.appendChild(piece);
                }
            }

            function activateHomeHeading(event) {
                celebrateHeadingClick(event);
                toggleHeadingSearch();
            }

            function currentUrlValue() {
                return location.pathname + location.search + location.hash;
            }

            function cancelAutoSearchHistory() {
                clearTimeout(autoSearchCommitTimer);
                autoSearchCommitTimer = null;
                autoSearchBase = null;
            }

            function beginAutoSearchHistory() {
                if (autoSearchBase) return;
                autoSearchBase = {
                    state: history.state,
                    url: currentUrlValue(),
                };
            }

            function commitAutoSearchHistory() {
                if (!autoSearchBase) return;
                const base = autoSearchBase;
                const searchUrl = currentUrlValue();
                autoSearchBase = null;
                clearTimeout(autoSearchCommitTimer);
                autoSearchCommitTimer = null;
                // Restore the entry the burst started from, then add the settled
                // query as its own entry. This avoids pushing a duplicate URL (and
                // the no-op Back press that comes with it).
                history.replaceState(base.state, "", base.url);
                if (searchUrl !== base.url) {
                    history.pushState(null, "", searchUrl);
                }
            }

            function scheduleAutoSearchHistoryCommit() {
                clearTimeout(autoSearchCommitTimer);
                autoSearchCommitTimer = setTimeout(() => {
                    autoSearchCommitTimer = null;
                    commitAutoSearchHistory();
                }, AUTO_HISTORY_DELAY);
            }

            function scheduleAutoSubmit() {
                clearTimeout(autoSubmitTimer);
                autoSubmitTimer = null;
                if (composing) return;
                if (!$("question").value.trim()) return;
                autoSubmitTimer = setTimeout(() => {
                    autoSubmitTimer = null;
                    if (composing) return;
                    submitSearch(false, { auto: true });
                }, AUTO_SUBMIT_DELAY);
            }

            function submitSearch(pushHistory, { auto = false } = {}) {
                const question = $("question").value.trim();
                if (!question) {
                    clearSearch(pushHistory);
                    return;
                }
                if (question.length > MAX_QUERY_LENGTH) {
                    $("searchButton").disabled = false;
                    $("status").textContent = "";
                    $("notice").textContent =
                        `Search queries are limited to ${MAX_QUERY_LENGTH.toLocaleString()} characters.`;
                    return;
                }
                // Keep the current source filter when the query changes, so a search
                // started from a library source's "Search" button stays scoped to it.
                const selectedSources = [...selectedSourceKeys];
                const url = makeStateUrl(question, selectedSources, 0);
                if (pushHistory) {
                    commitAutoSearchHistory();
                    if (url !== currentUrlValue()) {
                        history.pushState(null, "", url);
                    }
                } else if (auto) {
                    beginAutoSearchHistory();
                    history.replaceState(null, "", url);
                    scheduleAutoSearchHistoryCommit();
                } else {
                    // A deliberate search that should replace the current entry (for
                    // example, searching selected text from the article toolbar).
                    cancelAutoSearchHistory();
                    history.replaceState(null, "", url);
                }
                runSearch(question, selectedSources);
            }

            $("question").addEventListener("input", (event) => {
                if (composing || event.isComposing) return;
                scheduleAutoSubmit();
            });
            $("question").addEventListener("compositionstart", () => {
                composing = true;
                clearTimeout(autoSubmitTimer);
                autoSubmitTimer = null;
            });
            $("question").addEventListener("compositionend", () => {
                composing = false;
                scheduleAutoSubmit();
            });

            $("searchForm").addEventListener("submit", (event) => {
                event.preventDefault();
                clearTimeout(autoSubmitTimer);
                autoSubmitTimer = null;
                if (composing) return;
                submitSearch(true);
            });

            $("homeHeading").addEventListener("click", activateHomeHeading);

            // The active view link should be a no-op. Reloading it would drop
            // the current query/results (Search) or the library filter/shuffle
            // state (Library), which is surprising for an already-current tab.
            for (const link of document.querySelectorAll(".view-nav a")) {
                link.addEventListener("click", (event) => {
                    if (modifiedClick(event)) return;
                    const target = new URL(link.href, location.href);
                    if (target.pathname === location.pathname) {
                        event.preventDefault();
                    }
                });
            }

            window.addEventListener("popstate", () => {
                cancelAutoSearchHistory();
                const libraryView = location.pathname === appPath("/library");
                if (libraryView) {
                    const filter = readLibraryFilter();
                    if ($("libraryFilter").value !== filter) {
                        $("libraryFilter").value = filter;
                        renderLibrary();
                    }
                }
                const state = readLocationState();
                syncArticleModal(state.modal);
                if (!$("articleModal").hidden) {
                    closeLibraryInfo({ navigateHistory: false });
                } else {
                    syncLibraryInfo(history.state?.zimanticLibraryInfo || null);
                }
                if (!state.question) {
                    // Navigated back/forward to the no-search entry: match it.
                    resetResults();
                    $("question").value = "";
                    return;
                }
                $("question").value = state.question;
                const currentScope = current && !current.searchAll
                    ? [...new Set(current.searchKeys || [])]
                    : [];
                const stateScope = state.hasScope ? state.scopeSources : [];
                const sameScope =
                    currentScope.length === stateScope.length &&
                    currentScope.every((key) => stateScope.includes(key));
                if (current && current.question === state.question && sameScope) {
                    // Same query and search scope: reuse the pool when the
                    // displayed filters change; otherwise the scope itself
                    // changed and needs a new search.
                    applySourceSelection(state.question, state.selectedSources, state.page);
                } else {
                    runSearch(
                        state.question,
                        state.selectedSources,
                        state.page,
                        stateScope,
                    );
                }
            });

            async function initialize() {
                normalizePageParam();
                const libraryView = location.pathname === appPath("/library");
                if (libraryView) $("libraryFilter").value = readLibraryFilter();
                const state = readLocationState();
                // Apply the URL state before the discovery fetches below: a query typed
                // while they are in flight must keep the URL's source selection.
                setSelectedSources(state.selectedSources);
                $("homeHeading").setAttribute(
                    "aria-label",
                    libraryView
                        ? "Zimantic: switch to search"
                        : "Zimantic: clear or restore search",
                );
                try {
                    // Source discovery is read-only; refreshing the index/catalog set is an
                    // admin action done only via `zimantic reload` or SIGHUP.
                    const dataPath = libraryView ? appPath("/library.json") : appPath("/sources");
                    const dataCacheKey = libraryView ? LIBRARY_CACHE_KEY : SOURCE_CACHE_KEY;
                    const [configResponse, data] = await Promise.all([
                        fetch(appPath("/config"), { cache: "no-cache" }),
                        fetchCachedJson(dataPath, dataCacheKey),
                    ]);
                    if (configResponse.ok) {
                        const config = await configResponse.json();
                        debugMode = Boolean(config.debug);
                        if (Number.isInteger(config.page_size) && config.page_size > 0) {
                            PAGE_SIZE = config.page_size;
                        }
                        if (Array.isArray(config.stopwords)) {
                            HIGHLIGHT_STOPWORDS = new Set(
                                config.stopwords.map((word) => String(word)),
                            );
                        }
                        if (
                            Number.isInteger(config.source_filter_limit) &&
                            config.source_filter_limit > 0
                        ) {
                            SOURCE_FILTER_LIMIT = config.source_filter_limit;
                        }
                        if (
                            Number.isInteger(config.max_query_length) &&
                            config.max_query_length > 0
                        ) {
                            MAX_QUERY_LENGTH = config.max_query_length;
                            $("question").maxLength = MAX_QUERY_LENGTH;
                        }
                    }
                    if (libraryView) {
                        // Unreadable ZIMs stay visible so the card can explain
                        // why they cannot be opened. Readable archives without a
                        // homepage are also kept; the card can offer a random
                        // article when the source is searchable.
                        librarySources = sortLibrarySources(data.payload);
                        renderLibrary();
                    } else {
                        sources = data.payload;
                    }
                    if (data.stale) {
                        $("status").textContent =
                            `Using cached ${libraryView ? "library" : "source"} data; refresh failed: ${data.error.message}`;
                    }
                } catch (error) {
                    $("status").textContent =
                        `Could not load ${libraryView ? "library" : "sources"}: ${error.message}`;
                }
                $("searchView").hidden = libraryView;
                $("libraryView").hidden = !libraryView;
                document.body.classList.toggle("library-active", libraryView);
                for (const link of document.querySelectorAll(".view-nav a")) {
                    const isLibraryLink = link.getAttribute("href") === "library";
                    if (isLibraryLink === libraryView) {
                        link.setAttribute("aria-current", "page");
                    } else {
                        link.removeAttribute("aria-current");
                    }
                }
                syncArticleModal(state.modal);
                if (!$("articleModal").hidden) {
                    closeLibraryInfo({ navigateHistory: false });
                } else if (libraryView) {
                    syncLibraryInfo(history.state?.zimanticLibraryInfo || null);
                }
                if (state.question) {
                    $("question").value = state.question;
                    runSearch(
                        state.question,
                        state.selectedSources,
                        state.page,
                        state.scopeSources,
                    );
                } else if (!libraryView) {
                    renderSourceSelectionSummary();
                    $("question").focus();
                }
            }

            initialize();
        })();

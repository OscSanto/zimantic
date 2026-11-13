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
            const FOCUSABLE_SELECTOR =
                'a[href], button:not([disabled]):not([tabindex="-1"]), ' +
                "input:not([disabled]), select:not([disabled]), " +
                "textarea:not([disabled]), summary, " +
                '[tabindex]:not([tabindex="-1"])';

            function trapModalTabFocus(container, event) {
                if (event.key !== "Tab" || !container || container.hidden) return;
                const focusable = [...container.querySelectorAll(FOCUSABLE_SELECTOR)].filter(
                    (node) => node.getClientRects().length > 0,
                );
                if (!focusable.length) return;
                const first = focusable[0];
                const last = focusable[focusable.length - 1];
                if (!container.contains(document.activeElement)) {
                    event.preventDefault();
                    first.focus();
                } else if (
                    event.shiftKey &&
                    (document.activeElement === first || document.activeElement === container)
                ) {
                    event.preventDefault();
                    last.focus();
                } else if (!event.shiftKey && document.activeElement === last) {
                    event.preventDefault();
                    first.focus();
                }
            }

            function replaceQueryParam(name, value) {
                const url = new URL(location.href);
                if (value === null) {
                    if (!url.searchParams.has(name)) return;
                    url.searchParams.delete(name);
                } else {
                    if (url.searchParams.get(name) === value) return;
                    url.searchParams.set(name, value);
                }
                history.replaceState(
                    history.state,
                    "",
                    url.pathname + url.search + url.hash,
                );
            }

            let PAGE_SIZE = 10; // replaced with the server's page_size at startup
            let debugMode = false;
            // Filled from /config so highlighting uses the same stopword list as
            // the server instead of maintaining a second copy here.
            let HIGHLIGHT_STOPWORDS = new Set();
            const BROWSE_LABELS = {
                random: "Random",
                rare: "Rare titles",
                outliers: "Semantic outliers",
                diverse: "Diversity",
                recency: "Recency",
            };
            const BROWSE_MODES = new Set(Object.keys(BROWSE_LABELS));
            let availableBrowseModes = new Set(BROWSE_MODES);
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
            // A cold search on a Pi can run for a while before its first byte.
            // Once data starts, the server emits a progress line roughly every
            // 0.7 s, so a long silence means the transport stalled (mobile
            // proxy, buffering gateway), not that the search is merely slow. A
            // fixed wall-clock timeout used to abort healthy searches at 15 s
            // and leave the server finishing them in the background.
            // A waiter for an identical in-flight search can stay silent for
            // SEARCH_CLAIM_WAIT_SECONDS (60 s) before it replays the result, so
            // allow more than that before falling back to the JSON endpoint.
            const STREAM_FIRST_BYTE_TIMEOUT = 90000;
            const STREAM_IDLE_TIMEOUT = 30000;
            // Safety net for a stream that keeps sending progress but never
            // finishes. A healthy cold search is far shorter than this.
            const STREAM_TOTAL_TIMEOUT = 300000;
            const BUSY_RETRY_BASE_DELAY = 1000;
            const BUSY_RETRY_MAX_DELAY = 8000;
            const BUSY_RETRY_LIMIT = 5;
            const THUMB_LIMIT = 128 * 1024;
            const THUMB_CONCURRENCY = 4;
            let SOURCE_FILTER_LIMIT = 10;
            let COLLECTION_SOURCE_LIMIT = 1000;
            const SOURCE_FILTER_QUERY_LIMIT = 30;
            let sourceFilterShowAll = false;
            let sourceFilterShowOther = false;
            const SOURCE_CACHE_KEY = "zimantic.sources.v4";
            const LIBRARY_CACHE_KEY = "zimantic.library.v7";
            const COLLECTION_CACHE_KEY = "zimantic.collections.v1";
            const LIBRARY_PAGE_CACHE_KEY = "zimantic.library-pages.v1";
            const reducedMotion = () => matchMedia("(prefers-reduced-motion: reduce)").matches;
            let sources = [];
            let librarySources = [];
            let collections = [];
            let libraryCollections = [];
            const libraryCollectionPages = new Map();
            const libraryLanguageOptions = new Set();
            const libraryLanguageDisplayNames = new Map();
            let libraryCardCache = new WeakMap();
            const libraryCollectionRefreshes = new Map();
            let libraryLoadSequence = 0;
            let libraryFilterTimer = null;
            let current = null;
            let previousHeadingSearch = null;
            let selectedSourceKeys = new Set();
            let selectedCollectionKey = null;
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
                return String(source.title || source.key || "Untitled source").trim();
            }

            function sourceRecord(keyOrSource) {
                if (keyOrSource && typeof keyOrSource === "object") return keyOrSource;
                const wanted = String(keyOrSource || "").trim();
                if (!wanted) return null;
                const matches = (source) =>
                    String(source.key || "") === wanted;
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
                return String(source.key || "").trim();
            }

            function providerLabel(provider) {
                const value = String(provider || "");
                if (value === "zim") return "ZIM";
                if (value === "files") return t("Files");
                if (value === "kolibri") return t("Kolibri");
                if (value === "kiwix") return t("Remote Kiwix");
                return value;
            }

            function mediaTypeLabels(mediaTypes) {
                const labels = {
                    text: t("Text"),
                    video: t("Video"),
                    audio: t("Audio"),
                    image: t("Image"),
                    other: t("Other"),
                };
                return (Array.isArray(mediaTypes) ? mediaTypes : [])
                    .map((value) => labels[value] || String(value))
                    .filter(Boolean);
            }

            function collectionFor(sourceOrKey) {
                if (sourceOrKey && typeof sourceOrKey === "object") {
                    return sourceOrKey.collection || null;
                }
                const source = sourceRecord(sourceOrKey);
                return source?.collection || null;
            }

            function collectionKeyFor(source) {
                return String(source?.collection?.key || "");
            }

            function collectionLabelFor(sourceOrKey) {
                const collection = collectionFor(sourceOrKey);
                return String(collection?.label || collection?.title || "");
            }

            function collectionByKey(key) {
                const wanted = String(key || "");
                if (!wanted) return null;
                const direct =
                    collections.find((collection) => collection.key === wanted) ||
                    libraryCollections.find((collection) => collection.key === wanted);
                if (direct) return direct;
                const source = [...sources, ...librarySources].find(
                    (item) => item?.collection?.key === wanted,
                );
                return source?.collection || null;
            }

            function collectionDisplayName(keyOrCollection) {
                if (keyOrCollection && typeof keyOrCollection === "object") {
                    return String(
                        keyOrCollection.title ||
                            keyOrCollection.label ||
                            keyOrCollection.key ||
                            "Collection",
                    );
                }
                const collection = collectionByKey(keyOrCollection);
                return String(
                    collection?.title ||
                        collection?.label ||
                        keyOrCollection ||
                        "Collection",
                );
            }

            function librarySearchText(source) {
                const tags = Array.isArray(source.tags) ? source.tags : [];
                const collection = collectionFor(source);
                return [
                    source.title,
                    source.key,
                    source.provider,
                    providerLabel(source.provider),
                    source.description,
                    source.long_description,
                    source.creator,
                    source.publisher,
                    source.language,
                    source.date,
                    source.category,
                    source.license,
                    source.homepage,
                    source.native_fulltext_available,
                    source.searchable,
                    source.icon_url,
                    source.mime_counts,
                    source.item_count,
                    formatLibrarySize(source.size),
                    collection?.label,
                    collection?.title,
                    collection?.provider,
                    ...mediaTypeLabels(source.media_types),
                    ...tags,
                ]
                    .filter(Boolean)
                    .join(" ")
                    .toLocaleLowerCase();
            }

            function libraryHomepageUrl(source) {
                const zim = source.key;
                if (!zim) return "";
                if (source.homepage) return contentUrl(zim, source.homepage);
                if (source.provider === "files") return contentUrl(zim);
                // A readable archive can lack a resolvable main entry. If it is
                // searchable, open a random article instead of a broken
                // homepage link; otherwise the card has Details only.
                if (source.searchable !== false) {
                    return appPath(`/random?source=${encodeURIComponent(zim)}`);
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

            function libraryLanguageCodes(source) {
                return String(source?.language || "")
                    .split(",")
                    .map((part) => part.trim().toLocaleLowerCase())
                    .filter(Boolean);
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
                term.textContent = t(label);
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
                const key = source.key;
                if (!key) return;
                const target = new URL(makeStateUrl("", [key], 0), location.href);
                target.pathname = appPath("/");
                // The search view has no library filter.
                target.searchParams.delete("filter");
                target.searchParams.delete("provider");
                target.searchParams.delete("media");
                window.location.assign(target.toString());
            }

            function searchWithinLibraryCollection(collection) {
                const key = String(collection?.key || "").trim();
                if (!key) return;
                closeLibraryInfo({ navigateHistory: false });
                const target = new URL(appPath("/"), location.href);
                target.search = "";
                target.searchParams.set("collection", key);
                window.location.assign(target.toString());
            }

            function libraryInfoSourceKey(source) {
                return String(source.key || "");
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
                    source.creator && `${t("By")} ${source.creator}`,
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
                if (homepage || searchable) {
                    const actions = document.createElement("div");
                    actions.className = "library-info-actions";
                    if (homepage) {
                        const open = document.createElement("a");
                        open.className = "library-button library-info-action";
                        open.href = homepage;
                        open.dataset.libraryArticle = "true";
                        const label = source.homepage ? "Open" :
                            source.provider === "files" ? "Browse files" : "Random article";
                        open.textContent = label;
                        open.dataset.i18n = label;
                        actions.append(open);
                    }
                    if (searchable) {
                        const search = document.createElement("button");
                        search.type = "button";
                        search.className = "library-button library-info-action";
                        search.textContent = "Search";
                        search.dataset.i18n = "Search";
                        search.addEventListener("click", () => searchWithinLibrarySource(source));
                        actions.append(search);
                    }
                    content.appendChild(actions);
                } else {
                    const unavailable = document.createElement("p");
                    unavailable.className = "library-info-unavailable";
                    unavailable.textContent = t(
                        "This source has no homepage and cannot be searched yet.",
                    );
                    content.appendChild(unavailable);
                }

                const list = document.createElement("dl");
                list.className = "library-info-meta";
                appendInfoRow(list, "Publisher", source.publisher);
                appendInfoRow(list, "Creator", source.creator);
                appendInfoRow(list, "Language", libraryLanguage(source, true));
                appendInfoRow(list, "Date", source.date);
                appendInfoRow(list, "Size", formatLibrarySize(source.size));
                if (Number(source.item_count) > 0) {
                    appendInfoRow(list, "Items", Number(source.item_count).toLocaleString());
                }
                if (Number(source.indexed_count) > 0) {
                    appendInfoRow(list, "Indexed", Number(source.indexed_count).toLocaleString());
                }
                appendInfoRow(list, "Category", source.category);
                appendInfoRow(list, "Provider", providerLabel(source.provider));
                appendInfoRow(list, "Collection", collectionLabelFor(source));
                appendInfoRow(
                    list,
                    "Media types",
                    mediaTypeLabels(source.media_types).join(" · "),
                );
                appendInfoRow(
                    list,
                    "Tags",
                    Array.isArray(source.tags) ? source.tags.filter(Boolean).join(" · ") : "",
                );
                appendInfoRow(list, "License", source.license);
                appendInfoRow(list, "Origin", source.origin);
                appendInfoRow(list, "Attribution", source.attribution, { href: false });
                appendInfoRow(list, "Relation", source.relation);
                appendInfoRow(list, "Provider ID", source.provider_id);
                if (list.children.length) content.appendChild(list);

                const counter = counterSummary(source.mime_counts);
                if (counter.length) {
                    const details = document.createElement("details");
                    details.className = "library-info-counter";
                    const summary = document.createElement("summary");
                    summary.textContent = t("Media types");
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
                const shuffle = (values) => {
                    for (let index = values.length - 1; index > 0; index--) {
                        const other = Math.floor(Math.random() * (index + 1));
                        [values[index], values[other]] = [
                            values[other],
                            values[index],
                        ];
                    }
                };
                shuffle(libraryCollections);
                for (const state of libraryCollectionPages.values()) {
                    shuffle(state.sources);
                }
                librarySources = sortLibrarySources(
                    [...libraryCollectionPages.values()].flatMap(
                        (state) => state.sources,
                    ),
                );
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

            function isCanonicalSourceRecord(source) {
                return Boolean(
                    source &&
                    typeof source === "object" &&
                    typeof source.key === "string" &&
                    source.key &&
                    typeof source.provider === "string" &&
                    source.provider &&
                    !("mode" in source) &&
                    !("content_kind" in source) &&
                    !("local_name" in source) &&
                    !("book" in source) &&
                    !("name" in source),
                );
            }

            function readCanonicalCache(key) {
                const cached = readJsonCache(key);
                if (!cached || !cached.every(isCanonicalSourceRecord)) return null;
                return cached;
            }

            function isCanonicalCollectionRecord(collection) {
                return Boolean(
                    collection &&
                    typeof collection === "object" &&
                    typeof collection.key === "string" &&
                    collection.key &&
                    typeof collection.provider === "string" &&
                    collection.provider &&
                    typeof collection.label === "string",
                );
            }

            function readCollectionCache() {
                const cached = readJsonCache(COLLECTION_CACHE_KEY);
                if (!cached || !cached.every(isCanonicalCollectionRecord)) return null;
                return cached;
            }

            function listingParams({
                provider = "",
                media = "",
                language = "",
                q = "",
                collection = "",
            } = {}) {
                const params = new URLSearchParams();
                if (provider) params.set("provider", provider);
                if (media) params.set("media_type", media);
                if (language) params.set("language", language);
                if (q) params.set("q", q);
                if (collection) params.set("collection", collection);
                return params;
            }

            async function fetchJsonPage(
                path,
                {
                    limit = PAGE_SIZE,
                    offset = 0,
                    provider = "",
                    media = "",
                    language = "",
                    q = "",
                    collection = "",
                } = {},
            ) {
                const url = new URL(path, location.href);
                url.searchParams.set("limit", String(Math.max(1, limit)));
                url.searchParams.set("offset", String(Math.max(0, offset)));
                for (const [key, value] of listingParams({
                    provider,
                    media,
                    language,
                    q,
                    collection,
                })) {
                    url.searchParams.set(key, value);
                }
                const response = await fetch(url, { cache: "no-cache" });
                if (!response.ok) throw new Error(`HTTP ${response.status}`);
                const payload = await response.json();
                if (!Array.isArray(payload)) throw new Error("invalid JSON payload");
                return {
                    items: payload,
                    total: Number(response.headers.get("X-Total-Count") || payload.length),
                    offset: Number(response.headers.get("X-Offset") || offset),
                    hasMore: response.headers.get("X-Has-More") === "true",
                    version: response.headers.get("X-Snapshot-Version") || "",
                };
            }

            async function fetchPagedJson(path, options = {}) {
                const limit = Math.max(1, options.limit || PAGE_SIZE);
                const retries = Number(options.retries || 0);
                const items = [];
                let offset = 0;
                let version = "";
                for (let page = 0; page < 1000; page++) {
                    const result = await fetchJsonPage(path, {
                        ...options,
                        limit,
                        offset,
                    });
                    if (version && result.version && result.version !== version) {
                        // A discovery revision changed between pages. Restart
                        // once instead of mixing records from two snapshots;
                        // a revision that keeps moving surfaces an error.
                        if (retries < 1) {
                            return fetchPagedJson(path, {
                                ...options,
                                retries: retries + 1,
                            });
                        }
                        throw new Error(
                            "discovery changed while loading; refresh the library",
                        );
                    }
                    version = result.version || version;
                    items.push(...result.items);
                    if (!result.items.length || !result.hasMore) return items;
                    offset += result.items.length;
                }
                throw new Error("listing page count exceeded its bound");
            }

            async function fetchCachedJson(path, key, options = {}) {
                const cached = readCanonicalCache(key);
                try {
                    const payload = await fetchPagedJson(path, options);
                    if (!payload.every(isCanonicalSourceRecord)) {
                        throw new Error("invalid source payload");
                    }
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
                replaceQueryParam("filter", filter || null);
                replaceQueryParam(
                    "provider",
                    $("libraryProviderFilter")?.value || null,
                );
                replaceQueryParam("media", $("libraryMediaFilter")?.value || null);
                replaceQueryParam("language", $("libraryLanguageFilter")?.value || null);
            }

            function scheduleLibraryReload() {
                updateLibraryFilterUrl();
                renderLibraryScopeLine();
                clearTimeout(libraryFilterTimer);
                libraryFilterTimer = setTimeout(() => {
                    libraryFilterTimer = null;
                    loadLibraryData();
                }, 250);
            }

            function resetLibraryFilters() {
                $("libraryFilter").value = "";
                $("libraryProviderFilter").value = "";
                $("libraryMediaFilter").value = "";
                $("libraryLanguageFilter").value = "";
                clearTimeout(libraryFilterTimer);
                libraryFilterTimer = null;
                updateLibraryFilterUrl();
                renderLibraryScopeLine();
                loadLibraryData();
            }

            function sortLibrarySources(sources) {
                const titleCollator = new Intl.Collator(undefined, {
                    sensitivity: "base",
                    numeric: true,
                });
                return [...sources].sort((left, right) => {
                    const byTitle = titleCollator.compare(
                        libraryTitle(left),
                        libraryTitle(right),
                    );
                    if (byTitle) return byTitle;
                    return String(
                        left.key || "",
                    ).localeCompare(
                        String(right.key || ""),
                    );
                });
            }

            function libraryFilterTerms() {
                return $("libraryFilter")?.value.trim().toLocaleLowerCase() || "";
            }

            function matchesLibraryTerms(text, query) {
                if (!query) return true;
                const terms = query.split(/\s+/).filter(Boolean);
                const excluded = terms
                    .filter((term) => term.startsWith("-") && term.length > 1)
                    .map((term) => term.slice(1));
                const included = terms.filter(
                    (term) => !term.startsWith("-") || term.length === 1,
                );
                const haystack = String(text || "").toLocaleLowerCase();
                const matchesPrefix = (term) => {
                    const escaped = term.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
                    return new RegExp(
                        `(?:^|[^\\p{L}\\p{N}_])${escaped}`,
                        "u",
                    ).test(haystack);
                };
                return (
                    included.every(matchesPrefix) &&
                    excluded.every((term) => !matchesPrefix(term))
                );
            }

            function libraryCollectionText(collection) {
                return [
                    collection?.title,
                    collection?.label,
                    collection?.description,
                    collection?.provider,
                    providerLabel(collection?.provider),
                ]
                    .filter(Boolean)
                    .join(" ");
            }

            function libraryCardsFor(sources) {
                return sources.map((source) => {
                    const cached = libraryCardCache.get(source);
                    if (cached) return cached;
                    const title = libraryTitle(source);
                    const openUrl = libraryHomepageUrl(source);
                    const stem = sourceStemName(source);
                    const card = document.createElement("article");
                    card.className = "library-card";
                    const linkable = Boolean(openUrl);

                    const preview = document.createElement(linkable ? "a" : "div");
                    preview.className = "library-card-preview";
                    if (stem) preview.title = stem;
                    if (linkable) {
                        preview.href = openUrl;
                        preview.dataset.libraryArticle = "true";
                        preview.setAttribute("aria-label", `Open ${title}`);
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
                    const collectionLabel = [
                        providerLabel(source.provider),
                        collectionLabelFor(source),
                    ]
                        .filter(Boolean)
                        .join(" · ");
                    if (collectionLabel) {
                        const group = document.createElement("p");
                        group.className = "library-card-collection";
                        group.textContent = collectionLabel;
                        body.appendChild(group);
                    }
                    const description =
                        source.description ||
                        source.long_description ||
                        (linkable
                            ? ""
                            : t(
                                  "This source has no homepage and cannot be searched yet.",
                              ));
                    if (description) {
                        const text = document.createElement("p");
                        text.className = "library-card-description";
                        text.textContent = description;
                        body.appendChild(text);
                    }
                    const details = document.createElement("p");
                    details.className = "library-card-details";
                    const descriptiveDetails = [
                        libraryLanguageLabel(libraryLanguage(source)),
                        source.date || "",
                    ].filter(Boolean);
                    const inventoryDetails = [
                        Number(source.item_count) > 0
                            ? `${Number(source.item_count).toLocaleString()} ${
                                  Number(source.item_count) === 1
                                      ? "item"
                                      : "items"
                              }`
                            : "",
                        formatLibrarySize(source.size),
                    ].filter(Boolean);
                    const detailLines = [descriptiveDetails, inventoryDetails]
                        .filter((line) => line.length)
                        .map((line) => line.join(" · "));
                    details.textContent = detailLines.join("\n");
                    details.hidden = !detailLines.length;
                    body.appendChild(details);
                    if (source.creator) {
                        const creator = document.createElement("p");
                        creator.className = "library-card-creator";
                        creator.textContent = `${t("By")} ${source.creator}`;
                        body.appendChild(creator);
                    }
                    const actions = document.createElement("div");
                    actions.className = "library-card-actions";
                    const detailsButton = document.createElement("button");
                    detailsButton.type = "button";
                    detailsButton.className = "library-button library-card-action";
                    detailsButton.textContent = "Details";
                    detailsButton.dataset.i18n = "Details";
                    detailsButton.addEventListener("click", () => showLibraryInfo(source));
                    actions.appendChild(detailsButton);
                    body.appendChild(actions);
                    card.append(preview, body);
                    libraryCardCache.set(source, card);
                    return card;
                });
            }

            function libraryCollectionViews() {
                const query = libraryFilterTerms();
                const provider = $("libraryProviderFilter")?.value || "";
                const media = $("libraryMediaFilter")?.value || "";
                const language = $("libraryLanguageFilter")?.value || "";
                const orderedCollections =
                    libraryCollections.length || collections.length
                        ? libraryCollections.length
                            ? libraryCollections
                            : collections
                        : [];
                const views = [];
                const seen = new Set();
                for (const collection of orderedCollections) {
                    const key = String(collection.key || "");
                    if (!key || seen.has(key)) continue;
                    seen.add(key);
                    const state = libraryCollectionPages.get(key);
                    const collectionMatches = matchesLibraryTerms(
                        libraryCollectionText(collection),
                        query,
                    );
                    const members = (state?.sources || []).filter((source) => {
                        if (source.provider === "kolibri" && source.item_count === 0) {
                            return false;
                        }
                        if (provider && source.provider !== provider) return false;
                        if (
                            media &&
                            !(Array.isArray(source.media_types)
                                ? source.media_types
                                : []
                            ).includes(media)
                        ) {
                            return false;
                        }
                        if (
                            language &&
                            !libraryLanguageCodes(source).includes(language)
                        ) {
                            return false;
                        }
                        return (
                            collectionMatches ||
                            matchesLibraryTerms(librarySearchText(source), query)
                        );
                    });
                    views.push({
                        key,
                        collection,
                        state,
                        members,
                        collectionMatches,
                    });
                }
                for (const [key, state] of libraryCollectionPages.entries()) {
                    if (seen.has(key)) continue;
                    const collection =
                        collectionByKey(key) ||
                        state.collection || {
                            key,
                            label: key,
                            provider: "",
                            source_count: state.total,
                        };
                    views.push({
                        key,
                        collection,
                        state,
                        members: state.sources.filter(
                            (source) =>
                                (source.provider !== "kolibri" || source.item_count !== 0) &&
                                (!language ||
                                    libraryLanguageCodes(source).includes(language)),
                        ),
                        collectionMatches: true,
                    });
                }
                return views.filter((view) => {
                    if (!query && !provider && !media && !language) return true;
                    if (view.collectionMatches) {
                        if (!provider && !media && !language) return true;
                        if (
                            provider &&
                            view.collection?.provider &&
                            view.collection.provider !== provider
                        ) {
                            return false;
                        }
                        if (media || language) {
                            return view.members.length > 0 || !view.state || view.state.loading;
                        }
                        return true;
                    }
                    return view.members.length > 0;
                });
            }

            function libraryStateMessage(view) {
                const state = view.state;
                const collection = view.collection;
                if (state?.loading && !state.sources.length) {
                    return t("Discovering sources…");
                }
                if (state?.error && !state.sources.length) {
                    return `${t("Could not load this collection")}: ${state.error}`;
                }
                if (collection.discovery_state === "error" && collection.error) {
                    return `${t("Collection discovery failed")}: ${collection.error}`;
                }
                if (collection.discovery_state === "loading") {
                    return t("Discovering sources…");
                }
                if (!Number(collection.source_count) && !state?.sources.length) {
                    return t("No sources were discovered in this collection.");
                }
                if (!state && Number(collection.source_count)) {
                    return t("Loading sources…");
                }
                return t("No sources match these filters.");
            }

            function renderLibraryScopeLine() {
                const line = $("libraryScopeLine");
                const text = $("libraryScopeLineText");
                if (!line || !text) return;
                const filters = [];
                const provider = $("libraryProviderFilter")?.value || "";
                const media = $("libraryMediaFilter")?.value || "";
                const language = $("libraryLanguageFilter")?.value || "";
                const query = $("libraryFilter")?.value.trim() || "";
                if (provider) {
                    filters.push(`${t("Provider")}: ${providerLabel(provider)}`);
                }
                if (media) {
                    filters.push(`${t("Media type")}: ${mediaTypeLabels([media]).join("")}`);
                }
                if (language) {
                    filters.push(`${t("Language")}: ${libraryLanguageLabel(language)}`);
                }
                if (query) {
                    filters.push(`${t("Filter")}: ${query}`);
                }
                line.hidden = !filters.length;
                text.textContent = filters.length
                    ? `${t("Filtered by")}: ${filters.join(" · ")}`
                    : "";
            }

            function renderLibraryLanguageOptions() {
                const select = $("libraryLanguageFilter");
                if (!select) return;
                const selected = select.value || "";
                select.replaceChildren();
                const allLanguages = document.createElement("option");
                allLanguages.value = "";
                allLanguages.textContent = t("All languages");
                allLanguages.dataset.i18n = "All languages";
                select.appendChild(allLanguages);
                const locale = $("localePicker")?.value || "en";
                const options = [...libraryLanguageOptions]
                    .map((language) => ({ language, label: libraryLanguageLabel(language) }))
                    .sort((left, right) => left.label.localeCompare(right.label, locale));
                for (const { language, label } of options) {
                    const option = document.createElement("option");
                    option.value = language;
                    option.textContent = label;
                    select.appendChild(option);
                }
                select.value = selected;
            }

            function setAvailableLibraryFilterOptions(facets) {
                if (!facets || typeof facets !== "object") return;
                for (const [selectId, values] of [
                    ["libraryProviderFilter", facets.providers],
                    ["libraryMediaFilter", facets.media_types],
                ]) {
                    const select = $(selectId);
                    if (!select || !Array.isArray(values)) continue;
                    const available = new Set(values.map((value) => String(value)));
                    for (const option of select.options) {
                        if (option.value) option.hidden = !available.has(option.value);
                    }
                }
                if (Array.isArray(facets.languages)) {
                    libraryLanguageOptions.clear();
                    for (const value of facets.languages) {
                        const language = String(value || "").trim().toLocaleLowerCase();
                        if (language) libraryLanguageOptions.add(language);
                    }
                    renderLibraryLanguageOptions();
                }
            }

            function libraryLanguageName(language) {
                const locale = $("localePicker")?.value || "en";
                const fallbackNames = {
                    en: { lao: "Lao", ewe: "Ewe" },
                    es: { lao: "lao", ewe: "ewé" },
                };
                const fallback = (fallbackNames[locale] || fallbackNames.en)[language]
                    || language.toUpperCase();
                if (typeof Intl.DisplayNames !== "function") {
                    return fallback;
                }
                let displayNames = libraryLanguageDisplayNames.get(locale);
                if (!displayNames) {
                    try {
                        displayNames = new Intl.DisplayNames([locale], {
                            type: "language",
                        });
                        libraryLanguageDisplayNames.set(locale, displayNames);
                    } catch (_) {
                        return fallback;
                    }
                }
                try {
                    const name = displayNames.of(language);
                    return name && name.toLocaleLowerCase() !== language
                        ? name
                        : fallback;
                } catch (_) {
                    return fallback;
                }
            }

            function libraryLanguageLabel(code) {
                const language = String(code || "").trim().toLocaleLowerCase();
                if (!language) return "";
                const name = libraryLanguageName(language);
                return name === language.toUpperCase()
                    ? language.toUpperCase()
                    : `${name} (${language.toUpperCase()})`;
            }

            function setLibraryLanguageFilter(value) {
                const language = String(value || "").trim().toLocaleLowerCase();
                if (language) libraryLanguageOptions.add(language);
                renderLibraryLanguageOptions();
                const select = $("libraryLanguageFilter");
                if (select) select.value = language;
            }

            function renderLibrary() {
                const grid = $("libraryGrid");
                const status = $("libraryStatus");
                if (!grid || !status) return;
                renderLibraryScopeLine();
                const views = libraryCollectionViews();
                const loadedCount = views.reduce((count, view) => count + view.members.length, 0);
                const filter = $("libraryFilter");
                if (filter) {
                    const count = loadedCount;
                    filter.placeholder = `${t("Filter")} ${count.toLocaleString()} ${t(
                        count === 1 ? "source" : "sources",
                    )}`;
                }
                grid.replaceChildren();
                const hasFilters = Boolean(
                    libraryFilterTerms() ||
                        $("libraryProviderFilter")?.value ||
                        $("libraryMediaFilter")?.value ||
                        $("libraryLanguageFilter")?.value,
                );
                if (!views.length) {
                    status.textContent = hasFilters
                        ? `0 ${t("collections")} · 0 ${t("loaded sources")}`
                        : "";
                    const empty = document.createElement("p");
                    empty.className = "empty";
                    if (!collections.length && !hasFilters) {
                        empty.textContent = t(
                            "No collections are configured. Add a [[collections]] entry to the server configuration, then restart the server.",
                        );
                    } else if (hasFilters) {
                        empty.textContent = t(
                            "No collection or source matches these filters.",
                        );
                    } else {
                        empty.textContent = t(
                            "No sources are available in the configured collections.",
                        );
                    }
                    grid.appendChild(empty);
                    return;
                }
                status.textContent = `${views.length} ${t(
                    views.length === 1 ? "collection" : "collections",
                )} · ${loadedCount} ${t(
                    loadedCount === 1
                        ? "loaded source"
                        : "loaded sources",
                )}`;
                for (const view of views) {
                    const { key, collection, state, members } = view;
                    const section = document.createElement("section");
                    section.className = "library-collection";
                    section.dataset.libraryCollection = key;
                    const header = document.createElement("div");
                    header.className = "library-collection-header";
                    const heading = document.createElement("div");
                    heading.className = "library-collection-heading";
                    const title = document.createElement("h3");
                    title.textContent = collectionDisplayName(collection);
                    heading.appendChild(title);
                    const meta = document.createElement("p");
                    meta.className = "library-collection-meta";
                    const sourceCount = Number(
                        hasFilters
                            ? state?.total ?? members.length
                            : collection.source_count,
                    );
                    meta.textContent = [
                        providerLabel(collection.provider),
                        Number.isFinite(sourceCount)
                            ? `${sourceCount.toLocaleString()} ${t(
                                  sourceCount === 1 ? "source" : "sources",
                              )}`
                            : "",
                    ]
                        .filter(Boolean)
                        .join(" · ");
                    heading.appendChild(meta);
                    header.appendChild(heading);
                    if (collection.description) {
                        const description = document.createElement("p");
                        description.className = "library-collection-description";
                        description.textContent = collection.description;
                        header.appendChild(description);
                    }
                    const actions = document.createElement("div");
                    actions.className = "library-collection-actions";
                    if (Number(collection.source_count) > 0) {
                        const search = document.createElement("button");
                        search.type = "button";
                        search.className = "library-button";
                        search.textContent = "Search collection";
                        search.dataset.i18n = "Search collection";
                        search.dataset.librarySearchCollection = key;
                        actions.appendChild(search);
                    }
                    if (state?.hasMore) {
                        const more = document.createElement("button");
                        more.type = "button";
                        more.className = "library-button";
                        more.textContent = t("Load more sources");
                        more.dataset.libraryMoreCollection = key;
                        more.disabled = state.loading;
                        actions.appendChild(more);
                    }
                    header.appendChild(actions);
                    section.appendChild(header);

                    if (!members.length) {
                        const message = document.createElement("p");
                        message.className = "empty";
                        message.textContent = libraryStateMessage(view);
                        section.appendChild(message);
                    } else {
                        const groupGrid = document.createElement("div");
                        groupGrid.className = "library-grid library-collection-grid";
                        groupGrid.append(...libraryCardsFor(members));
                        section.appendChild(groupGrid);
                        if (state?.hasMore) {
                            const note = document.createElement("p");
                            note.className = "library-collection-note";
                            note.textContent = `${t("Showing")} ${members.length.toLocaleString()} ${t(
                                "of",
                            )} ${Number(
                                state.total,
                            ).toLocaleString()} ${t("sources")}.`;
                            section.appendChild(note);
                        }
                    }
                    grid.appendChild(section);
                }
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
                return sources;
            }

            function selectionValues(values) {
                if (values instanceof Set) return [...values].filter(Boolean);
                return (Array.isArray(values) ? values : values ? [values] : []).filter(Boolean);
            }

            function t(value) {
                const i18n = typeof window !== "undefined" ? window.zimanticI18n : null;
                return i18n && typeof i18n.translate === "function"
                    ? i18n.translate(value)
                    : value;
            }

            function dedupeParams(params, name) {
                const values = [...new Set(params.getAll(name))];
                if (!values.length) return;
                params.delete(name);
                for (const value of values) params.append(name, value);
            }

            function applyLocationScope(state) {
                setSelectedSources(state.selectedSources);
                selectedCollectionKey = state.collection || null;
            }

            function effectiveSourceProgress() {
                if (!current) return { completed: 0, total: 0, selected: false };
                const selected = [...selectedSourceKeys].filter(
                    (key) => current.question || (current.searchKeys || []).includes(key),
                );
                if (selected.length) {
                    const known = new Set(
                        [...current.outcomes.keys()].filter((key) =>
                            selectedSourceKeys.has(key),
                        ),
                    );
                    return {
                        completed: known.size,
                        total: selected.length,
                        selected: true,
                    };
                }
                return {
                    completed: current.completed,
                    total: current.sourcesTotal,
                    selected: false,
                };
            }

            function nextSearchScope(selectedSources) {
                return [...new Set(selectionValues(selectedSources))];
            }

            function sourceDiscovery(question, search, browse = null, seed = 0) {
                if (
                    !search ||
                    search.question !== question ||
                    (search.browse || null) !== browse ||
                    (search.seed || 0) !== seed
                ) return null;
                if (!search.searchAll) return search.discovery || null;
                return {
                    outcomes: new Map(search.outcomes),
                    counts: { ...search.counts },
                };
            }

            function setSelectedSources(values) {
                selectedSourceKeys = new Set(selectionValues(values));
            }

            function searchScopeParams(params, keys, collectionKey = null) {
                if (collectionKey) params.set("collection", collectionKey);
                keys.forEach((key) => params.append("source", key));
            }

            function sourceFilterLayout() {
                // Only an earlier unscoped search of this query supplies extra
                // chips. Its counts are discovery hints, not current progress.
                const outcomes = [...new Map([
                    ...(current.discovery?.outcomes || []),
                    ...current.outcomes,
                ]).values()];
                const searched = new Set(current.searchKeys || []);
                const outcomeCount = (outcome) => {
                    const key = outcome.source.key;
                    return searched.has(key) || current.outcomes.has(key)
                        ? current.counts[key] || 0
                        : current.discovery?.counts[key] || 0;
                };
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
                const byCollection = new Map();
                for (const outcome of matching) {
                    const collection = outcome.source.collection || null;
                    const key = collection?.key || `provider:${outcome.source.provider || ""}`;
                    if (!byCollection.has(key)) {
                        byCollection.set(key, {
                            key,
                            label: collection?.label || outcome.source.provider || "",
                            provider: collection?.provider || outcome.source.provider || "",
                            outcomes: [],
                        });
                    }
                    byCollection.get(key).outcomes.push(outcome);
                }
                // A selected source must remain visible even when the source
                // filter query no longer matches its metadata.
                for (const outcome of outcomes) {
                    if (!selectedSourceKeys.has(outcome.source.key)) continue;
                    const collection = outcome.source.collection || null;
                    const key = collection?.key || `provider:${outcome.source.provider || ""}`;
                    if (!byCollection.has(key)) {
                        byCollection.set(key, {
                            key,
                            label: collection?.label || outcome.source.provider || "",
                            provider: collection?.provider || outcome.source.provider || "",
                            outcomes: [],
                        });
                    }
                    const group = byCollection.get(key);
                    if (!group.outcomes.includes(outcome)) {
                        group.outcomes.push(outcome);
                    }
                }
                const groups = [...byCollection.values()].map((group) => {
                    group.outcomes.sort((left, right) => {
                        const countDifference =
                            outcomeCount(right) - outcomeCount(left);
                        return (
                            countDifference ||
                            sourceDisplayName(left.source).localeCompare(
                                sourceDisplayName(right.source),
                            )
                        );
                    });
                    const limit = query
                        ? sourceFilterShowAll
                            ? group.outcomes.length
                            : SOURCE_FILTER_QUERY_LIMIT
                        : SOURCE_FILTER_LIMIT;
                    const selected = group.outcomes.filter((outcome) =>
                        selectedSourceKeys.has(outcome.source.key),
                    );
                    const showable = (outcome) =>
                        failed(outcome) || outcomeCount(outcome) > 0;
                    const nonSelected = group.outcomes.filter(
                        (outcome) =>
                            !selected.includes(outcome) && showable(outcome),
                    );
                    let visible = [...selected, ...nonSelected.slice(0, limit)];
                    const visibleSet = new Set(visible);
                    let other = group.outcomes.filter(
                        (outcome) =>
                            !visibleSet.has(outcome) && showable(outcome),
                    );
                    if (!query && sourceFilterShowOther) {
                        visible = group.outcomes.filter(showable);
                        other = [];
                    } else if (other.length === 1) {
                        // A lone hidden source is cheaper as a normal chip
                        // than as a one-item disclosure.
                        visible.push(other[0]);
                        other = [];
                    }
                    return {
                        ...group,
                        visible,
                        other,
                        showOther: other.length > 0,
                        otherCount: other.reduce(
                            (total, outcome) => total + outcomeCount(outcome),
                            0,
                        ),
                    };
                });
                const visible = groups.flatMap((group) => group.visible);
                const otherOutcomes = groups.flatMap((group) => group.other);
                const otherCount = groups.reduce(
                    (total, group) => total + group.otherCount,
                    0,
                );
                return {
                    outcomes,
                    query,
                    matching,
                    groups,
                    visible,
                    otherOutcomes,
                    otherCount,
                    showOther: otherOutcomes.length > 0,
                    failed,
                    outcomeCount,
                };
            }

            function sourceFilterControlKey(node) {
                if (!(node instanceof HTMLElement)) return "";
                if (node.dataset.sourceCollection) {
                    return `collection:${node.dataset.sourceCollection}`;
                }
                if (node.dataset.sourceAction) return `action:${node.dataset.sourceAction}`;
                if (node.dataset.source) return `source:${node.dataset.source}`;
                return "";
            }

            function sourceFilterButton(outcome, layout) {
                // Unselected discovery chips retain their earlier broad-pool
                // counts; selected sources show counts from the deeper search.
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
            }

            function renderSourceFilters() {
                const panel = $("sourceFilters");
                const toolbar = $("sourceFilterToolbar");
                const options = $("sourceFilterOptions");
                const status = $("sourceFilterStatus");
                // Before the stream's `started` event, the source scope is not
                // known yet. Showing an empty filter panel alongside
                // "Searching for …" is just noise.
                const scopeLoading = Boolean(
                    current?.streaming && !current.sourcesTotal,
                );
                if (!current || scopeLoading) {
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

                const entries = [];
                for (const group of layout.groups) {
                    const section = document.createElement("div");
                    section.className = "source-filter-group";
                    if (selectedCollectionKey === group.key) {
                        section.classList.add("selected");
                    }
                    const header = document.createElement("button");
                    header.type = "button";
                    header.className = "source-filter-collection";
                    header.dataset.sourceCollection = group.key;
                    header.setAttribute(
                        "aria-pressed",
                        selectedCollectionKey === group.key ? "true" : "false",
                    );
                    header.textContent = group.label;
                    header.title = `Search within ${group.label}`;
                    section.appendChild(header);
                    const groupOptions = document.createElement("div");
                    groupOptions.className = "source-filter-group-options";
                    groupOptions.append(
                        ...group.visible.map((outcome) =>
                            sourceFilterButton(outcome, layout),
                        ),
                    );
                    if (group.showOther) {
                        const other = document.createElement("button");
                        other.className = "source-filter-option ok";
                        other.type = "button";
                        other.dataset.sourceAction = layout.query ? "more" : "other";
                        if (!layout.query) {
                            other.setAttribute("aria-pressed", "false");
                        }
                        if (layout.query) {
                            other.textContent =
                                `${t("Show")} ${group.other.length} ${t("more")}`;
                        } else {
                            const count = group.other.length;
                            other.textContent =
                                `${t("Other")}: ${count} ${t(
                                    count === 1 ? "source" : "sources",
                                )}`;
                            other.title =
                                `${t("Show")} ${count} ${t("more")} ` +
                                `${t("sources")} (${group.otherCount} ${t("results")})`;
                        }
                        groupOptions.appendChild(other);
                    }
                    section.appendChild(groupOptions);
                    entries.push(section);
                }
                options.replaceChildren(...entries);
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
                        ? t("No matching sources.")
                        : current.streaming ||
                            layout.outcomes.length ||
                            !current.sourcesTotal
                          ? ""
                          : t("No sources returned results.");
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
                if (current || (!selectedSourceKeys.size && !selectedCollectionKey)) {
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
                const entries = [];
                if (selectedCollectionKey) {
                    const collection = document.createElement("button");
                    collection.className = "source-filter-option ok";
                    collection.type = "button";
                    collection.dataset.sourceCollection = selectedCollectionKey;
                    collection.setAttribute("aria-pressed", "true");
                    collection.textContent = collectionDisplayName(selectedCollectionKey);
                    entries.push(collection);
                }
                entries.push(...[...selectedSourceKeys].map((key) => {
                    const button = document.createElement("button");
                    button.className = "source-filter-option ok";
                    button.type = "button";
                    button.dataset.source = key;
                    button.setAttribute("aria-pressed", "true");
                    button.textContent = sourceDisplayName(key);
                    const stem = sourceStemName(key);
                    if (stem) button.title = stem;
                    return button;
                }));
                options.replaceChildren(...entries);
                const count = selectedSourceKeys.size || (selectedCollectionKey ? 1 : 0);
                const collectionMode = selectedCollectionKey && !selectedSourceKeys.size;
                const noun = collectionMode
                    ? count === 1
                        ? "collection"
                        : "collections"
                    : count === 1
                      ? "source"
                      : "sources";
                status.textContent =
                    `${count} ${t("selected")} ${t(noun)}. ` +
                    t(
                        count === 1
                            ? "Type a query to search it."
                            : "Type a query to search them.",
                    );
            }

            function renderScopeLine() {
                const line = $("scopeLine");
                const text = $("scopeLineText");
                const reset = $("scopeLineReset");
                if (!line || !text) return;
                if (current?.streaming && !current.sourcesTotal) {
                    line.hidden = true;
                    reset.hidden = true;
                    text.textContent = "";
                    return;
                }
                const collectionKey =
                    selectedCollectionKey || current?.searchCollection || null;
                const selected = [...selectedSourceKeys];
                const searchKeys = selected.length
                    ? selected
                    : current?.searchKeys || [];
                const scoped = Boolean(
                    collectionKey ||
                        selected.length ||
                        (current &&
                            !current.searchAll &&
                            searchKeys.length),
                );
                line.hidden = !scoped;
                reset.hidden = !scoped;
                if (!scoped) {
                    text.textContent = "";
                    return;
                }
                if (collectionKey) {
                    const count =
                        current?.searchCollection === collectionKey
                            ? current.searchKeys?.length || 0
                            : 0;
                    text.textContent =
                        `${t("Searching collection")}: ${collectionDisplayName(collectionKey)}` +
                        (count
                            ? ` (${count} ${t(count === 1 ? "source" : "sources")})`
                            : "");
                    return;
                }
                const names = searchKeys.map((key) => sourceDisplayName(key));
                const shown = names.slice(0, 3);
                const extra = names.length - shown.length;
                text.textContent =
                    names.length === 1
                        ? `${t("Searching only in")}: ${shown[0]}`
                        : `${t("Searching only in")} ${names.length} ${t("sources")}: ${shown.join(", ")}${
                              extra > 0
                                  ? ` ${t("and")} ${extra} ${t("more")}`
                                  : ""
                          }`;
            }

            function renderFilterLine(total) {
                $("filterLine").innerHTML = total > 0
                    ? '<hr class="source-results-break" aria-hidden="true">'
                    : "";
            }

            function sourceDisplayNameWithCollection(keyOrSource) {
                const name = sourceDisplayName(keyOrSource);
                const source = sourceRecord(keyOrSource);
                const label = collectionLabelFor(source || keyOrSource);
                if (!source || !label) return name;
                const candidates = current
                    ? [...current.outcomes.values()].map((outcome) => outcome.source)
                    : [...sources, ...librarySources];
                const duplicate = candidates.some(
                    (candidate) =>
                        candidate.key !== source.key &&
                        libraryTitle(candidate) === name,
                );
                return duplicate ? `${name} · ${label}` : name;
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
                source.textContent = sourceDisplayNameWithCollection(sourceKey);
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
                if (Array.isArray(item.breadcrumbs) && item.breadcrumbs.length) {
                    return item.breadcrumbs.join(" › ");
                }
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
                url.title = label;
                url.hidden = !label;
            }

            function resourceDetails(item) {
                const resource = item.resource;
                if (!resource) return [];
                const details = [];
                const add = (label, value) => {
                    if (value) details.push(`${t(label)}: ${value}`);
                };
                add("Grade level", resource.grade_levels?.join(", "));
                if (Number.isInteger(resource.duration) && resource.duration > 0) {
                    const hours = Math.floor(resource.duration / 3600);
                    const minutes = Math.floor((resource.duration % 3600) / 60);
                    const seconds = String(resource.duration % 60).padStart(2, "0");
                    add("Duration", hours
                        ? `${hours}:${String(minutes).padStart(2, "0")}:${seconds}`
                        : `${minutes}:${seconds}`);
                }
                add("Language", resource.language);
                add("Author", resource.author);
                if (Number.isInteger(resource.assessment_count) && resource.assessment_count >= 0) {
                    details.push(`${resource.assessment_count} ${t(resource.assessment_count === 1 ? "question" : "questions")}`);
                }
                if (
                    Number.isInteger(resource.topic_position) &&
                    Number.isInteger(resource.topic_total) &&
                    resource.topic_position > 0 &&
                    resource.topic_position <= resource.topic_total
                ) {
                    add("Position in topic", `${resource.topic_position} / ${resource.topic_total}`);
                }
                add("Tags", resource.tags?.join(", "));
                add("Learning activities", resource.learning_activities?.map(t).join(", "));
                return details;
            }

            function isHtmlMimetype(mimetype) {
                const mime = String(mimetype || "").toLowerCase();
                return mime === "text/html" || mime === "application/xhtml+xml";
            }

            function mediaBadge(mimetype, kind = null) {
                const kinds = {
                    exercise: "Exercise", html5: "Interactive", h5p: "Interactive",
                    document: "Document", video: "Video", audio: "Audio", slideshow: "Slideshow",
                };
                if (Object.hasOwn(kinds, kind)) {
                    const label = t(kinds[kind]);
                    return { icon: label, label };
                }
                const mime = String(mimetype || "").toLowerCase();
                if (!mime || mime === "text/html" || mime === "application/xhtml+xml") return null;
                let label = "File";
                if (mime.startsWith("video/")) label = "Video";
                else if (mime.startsWith("audio/")) label = "Audio";
                else if (mime.startsWith("image/")) label = "Image";
                else if (mime === "application/pdf") label = "PDF";
                else if (mime.startsWith("text/")) label = "Text";
                label = t(label);
                return { icon: label, label };
            }

            function appendMediaBadge(title, mimetype, kind = null) {
                const media = mediaBadge(mimetype, kind);
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
                    // /random?source=... redirects to a /content/ shell. Accept
                    // it while the iframe resolves so a library card without a
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
                let frame = $("articleModalFrame");
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
                    // A fresh browsing context keeps child-frame history and
                    // late messages from the previous article out of Back/Forward.
                    frame = replaceArticleFrame(articleUrl);
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
                // Count the article entries above the search/library entry so
                // Close can return there in one step instead of leaving a
                // duplicate entry that makes the next Back appear to do nothing.
                const depth = Number(history.state?.zimanticModalDepth);
                state.zimanticModalDepth =
                    Number.isInteger(depth) && depth >= 0 ? depth + 1 : 0;
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
                // through everything the reader visited. Close returns to the
                // entry where that trail started, leaving the visited articles
                // available in Forward history instead of pinning a duplicate
                // entry on top of the search page.
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
                return link ? rememberSearchResultUrl(link.href) : false;
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
                if (activeSearchResult.page !== current.page) {
                    return {
                        ...unavailable,
                        previousAvailable: activeSearchResult.page > 0,
                        nextAvailable:
                            activeSearchResult.page < current.pageCount - 1,
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
                        nextAvailable:
                            activeSearchResult.page < current.pageCount - 1,
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
                    nextAvailable:
                        Boolean(next) ||
                        activeSearchResult.page < current.pageCount - 1,
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
                const page = activeSearchResult.page + (direction === "next" ? 1 : -1);
                if (page < 0 || page >= current.pageCount) return;
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
                if (ranking.sort) {
                    appendDebugField(grid, "sort mode", ranking.sort.mode);
                    appendDebugField(grid, "sort score", debugNumber(ranking.sort.score));
                }
                if (ranking.browse) {
                    appendDebugField(grid, "browse mode", ranking.browse.mode);
                    appendDebugField(grid, "browse score", debugNumber(ranking.browse.score));
                    details.appendChild(grid);
                    return;
                }
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
                if (!debugMode || !current?.matchedBy || !current.question) {
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
                const frameLocation = () => {
                    // A captured page can move this frame somewhere we cannot
                    // read. Only a definite about:blank result below is treated
                    // as a discarded document.
                    try {
                        return frame.contentWindow?.location?.href || "";
                    } catch (_) {
                        return "";
                    }
                };
                frame.addEventListener("load", () => {
                    const modal = $("articleModal");
                    const activeFrame = $("articleModalFrame");
                    if (frame !== activeFrame || modal.hidden) return;
                    // iOS WebKit can discard an iframe document while traversing
                    // joint session history, leaving the modal visible over an
                    // empty frame and without a popstate event. Once this frame
                    // has shown an article, an about:blank load means the
                    // browser walked back out of it, so close the modal instead
                    // of leaving a blank screen.
                    const frameUrl = frameLocation();
                    if (frameUrl === "about:blank" || frameUrl === "about:srcdoc") {
                        if (frame.__zimanticArticleLoaded) closeArticleModal();
                        return;
                    }
                    if (!activeFrame.src || activeFrame.src === "about:blank") return;
                    frame.__zimanticArticleLoaded = true;
                    postArticleModalNavigation();
                });
            }

            function replaceArticleFrame(articleUrl = null) {
                const old = $("articleModalFrame");
                if (!old) return;
                const fresh = old.cloneNode(false);
                fresh.removeAttribute("src");
                if (articleUrl) fresh.src = articleUrl;
                bindArticleFrameLoad(fresh);
                old.replaceWith(fresh);
                return fresh;
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
                const state = history.state;
                const depth = Number(state?.zimanticModalDepth);
                const tracked = Boolean(state?.zimanticModal) &&
                    Number.isInteger(depth) && depth >= 0;
                if (navigateHistory && tracked) {
                    // Return to the search/library entry that started this
                    // article trail. This consumes the pushed modal entry, so
                    // the next Back leaves the search instead of landing on an
                    // identical URL and looking broken. The visited articles
                    // remain reachable with Forward.
                    hideArticleModal();
                    history.go(-(depth + 1));
                    return;
                }
                if (navigateHistory && new URL(location.href).searchParams.has("modal")) {
                    // A direct /?modal=... link has no in-app trail to return
                    // to. Clear the modal from this entry instead of walking
                    // out of the application.
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
                if (event.data?.type === "zimantic-external-redirect") {
                    // The content shell redirected to a running Kolibri server.
                    // That server forbids framing, so leave the modal and send
                    // the whole page there instead of showing a frame error.
                    const url = readExternalUrl(event.data.url);
                    if (!url) return;
                    // The whole page is leaving for the external server, so do
                    // not schedule a history walk that races the navigation.
                    closeArticleModal({ navigateHistory: false });
                    location.assign(url.href);
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
                node.classList.toggle("result-linked", Boolean(item.url));
                const title = node.querySelector(".result-title");
                title.replaceChildren();
                let displayTitle = item.title || "(untitled)";
                if (item.url) {
                    const link = document.createElement("a");
                    link.href = item.url;
                    link.target = "_blank";
                    link.rel = "noopener";
                    if (item.mimetype) link.dataset.mimetype = item.mimetype;
                    const label = document.createElement("span");
                    label.className = "result-title-text";
                    label.textContent = displayTitle;
                    link.appendChild(label);
                    title.appendChild(link);
                } else {
                    title.textContent = displayTitle;
                }
                appendMediaBadge(title, item.mimetype, item.resource?.kind);
                setSourceMeta(node, item);
                setResultUrl(node, item);
                const details = node.querySelector(".result-resource");
                details.textContent = resourceDetails(item).join(" · ");
                details.hidden = !details.textContent;
                const snippet = node.querySelector(".result-snippet");
                const lead = item.lead || item.resource?.description || "";
                // While a search is streaming, previews have not been extracted
                // yet. Show only the title and breadcrumb rows rather than a
                // placeholder, so each pending result stays roughly 3em tall.
                const pendingPreview = !lead && Boolean(current?.streaming);
                snippet.hidden = pendingPreview;
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
                    snippet.className = pendingPreview ? "result-snippet" : "result-snippet none";
                    snippet.textContent = pendingPreview
                        ? ""
                        : t("No preview available for this result.");
                }
                setResultDebug(node, item);
                let thumb = node.querySelector(".result-thumb");
                // Thumbnails are only fetched once the stream is done, so an
                // empty slot would stretch a pending row well past 3em.
                if (item.url && !pendingPreview) {
                    if (!thumb) {
                        thumb = document.createElement("a");
                        thumb.className = "result-thumb";
                        node.appendChild(thumb);
                    }
                    thumb.href = item.url;
                    thumb.target = "_blank";
                    thumb.rel = "noopener";
                    thumb.setAttribute("aria-label", displayTitle);
                    if (item.mimetype) {
                        thumb.dataset.mimetype = item.mimetype;
                    } else {
                        delete thumb.dataset.mimetype;
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
  <h3 class="result-title"></h3>
  <div class="result-meta"></div>
  <div class="result-resource" hidden></div>
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
                        : t(
                              current && !current.question
                                  ? "No indexed items are available for this sort order in the selected sources."
                                  : "No results found in the available sources. Try using different words.",
                          );
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

            function clearPartialResults() {
                // A stream can paint provisional results before a source or the
                // connection fails. Leaving them beside "Search failed" makes
                // them look complete, so drop the partial page and its counts.
                if (resultList) {
                    resultList.remove();
                    resultList = null;
                }
                lastRenderedItems = null;
                $("results").replaceChildren();
                const empty = document.createElement("div");
                empty.className = "empty";
                $("results").appendChild(empty);
                $("pager").innerHTML = "";
                $("sourceFilters").hidden = true;
                $("sourceFilterOptions").replaceChildren();
                $("sourceFilterStatus").textContent = "";
                $("filterLine").innerHTML = "";
                if (current) {
                    current.pageItems = [];
                    current.total = 0;
                    current.pageCount = 1;
                    current.matchedBy = null;
                }
                renderMatchedBy();
            }

            function pageRangeText(total, page) {
                if (!total) return t("No results");
                const from = page * PAGE_SIZE + 1;
                const to = Math.min((page + 1) * PAGE_SIZE, total);
                return `${from.toLocaleString()}–${to.toLocaleString()} ${t("of")} ${total.toLocaleString()} ${t(
                    total === 1 ? "result" : "results",
                )}`;
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
                const progress = effectiveSourceProgress();
                let range = current.total
                    ? pageRangeText(current.total, current.page)
                    : progress.total
                      ? t("No results")
                      : t("No searchable sources.");
                if (current.browse) {
                    range = `${t(BROWSE_LABELS[current.browse] || "Relevance")} · ${range}`;
                }
                // Keep the source tally visible once the stream has finished, on
                // every page, so paging does not drop it from the status line.
                return progress.total > 0 && progress.completed === progress.total
                    ? `${range} · ${progress.completed}/${progress.total} ${t(
                          progress.total === 1
                              ? "source complete"
                              : "sources complete",
                      )}`
                    : range;
            }

            function sourceProgressText(completed, total) {
                return `${completed}/${total} ${t(
                    total === 1 ? "source complete" : "sources complete",
                )}`;
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
                const progress = effectiveSourceProgress();
                // Before the stream's `started` event, 0/0 is not a result:
                // announcing "No searchable sources." here would be wrong.
                if (current.streaming && progress.total === 0) {
                    node.textContent = "";
                    return;
                }
                if (progress.total === 0) {
                    node.textContent = current.total
                        ? searchStatusText()
                        : current.streaming
                          ? ""
                          : t("No sources are available to search.");
                    return;
                }
                node.textContent =
                    progress.completed === progress.total
                        ? searchStatusText()
                        : "";
            }

            function renderPager(total, pageCount) {
                const pager = $("pager");
                const focused = document.activeElement;
                const focusAction =
                    focused instanceof HTMLElement && pager.contains(focused)
                        ? focused.dataset.pagerAction || ""
                        : "";
                pager.innerHTML = "";
                if (pageCount <= 1) {
                    if (focusAction) {
                        const firstResult = $("results").querySelector(
                            ".result-title a[href]",
                        );
                        if (firstResult) firstResult.focus({ preventScroll: true });
                    }
                    return;
                }
                const pageLink = (action, text, page, disabled) => {
                    const link = document.createElement(disabled ? "span" : "a");
                    link.dataset.pagerAction = action;
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
                const prev = pageLink(
                    "prev",
                    `← ${t("Previous")}`,
                    current.page - 1,
                    current.page === 0,
                );
                const next = pageLink(
                    "next",
                    `${t("Next")} →`,
                    current.page + 1,
                    current.page >= pageCount - 1,
                );
                const label = document.createElement("span");
                const from = total ? current.page * PAGE_SIZE + 1 : 0;
                const to = Math.min((current.page + 1) * PAGE_SIZE, total);
                label.textContent = `${from.toLocaleString()}–${to.toLocaleString()} ${t("of")} ${total.toLocaleString()}`;
                pager.append(prev, label, next);
                if (current.page > 0) {
                    // Shortcut back to the first page from anywhere in the pager.
                    pager.prepend(pageLink("first", `« ${t("First")}`, 0, false));
                }
                if (focusAction) {
                    const links = [...pager.querySelectorAll("a[data-page]")];
                    // A rebuild can remove the focused control (First on page 0)
                    // or turn it into a disabled span (Previous on page 0, Next
                    // on the last page). Fall back to the nearest enabled link
                    // in the same direction.
                    const target =
                        links.find(
                            (link) => link.dataset.pagerAction === focusAction,
                        ) ||
                        (focusAction === "next"
                            ? links[links.length - 1]
                            : links[0]);
                    if (target) target.focus({ preventScroll: true });
                }
            }

            function renderView() {
                if (!current) return;
                renderSortControls();
                // The server already sliced this page, clamped any out-of-range
                // offset, and reported the resulting page count.
                renderSourceFilters();
                renderScopeLine();
                renderFilterLine(current.total);
                renderPager(current.total, current.pageCount);
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
                const selected = selectedSourceKeys;
                const outcomes = [...current.outcomes.values()].filter(
                    (outcome) =>
                        !selected.size || selected.has(outcome.source.key),
                );
                const progress = effectiveSourceProgress();
                // Only a hard "error" status counts as a failure; a "partial"
                // source still returned results, so it must not trip this banner.
                const errors = outcomes.filter((outcome) => outcome.status === "error");
                $("notice").innerHTML =
                    progress.completed === progress.total &&
                    outcomes.length > 0 &&
                    errors.length === outcomes.length
                        ? `<div class='notice'><strong>${t("All sources failed.")}</strong> ${t(
                              "Check your sources and the Kiwix service, then reload or restart the server.",
                          )}</div>`
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

            function retryStatusText(status, delay) {
                const reason = status === 504 ? "Search gateway timed out" : "Too many searches are running";
                return `${reason}; retrying in ${delay / 1000}s…`;
            }

            async function consumeStream(response, sequence, onPart) {
                if (!response.body) throw new Error("streaming response has no body");
                const reader = response.body.getReader();
                const decoder = new TextDecoder();
                let buffer = "";
                let busy = false;
                // Paint the first ranked snapshot immediately. Later snapshots
                // coalesce on the slow render cadence, but a fast search should
                // not wait for that timer just to show its first results.
                let renderedSnapshot = false;
                const applyPage = (event) => {
                    current.completed = event.completed;
                    // A snapshot whose page didn't move omits `results`; keep the
                    // page we already have instead of clearing it.
                    if (Array.isArray(event.results)) current.pageItems = event.results;
                    current.total = event.total || 0;
                    current.pageCount = event.page_count || 1;
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
                            if (
                                event.all_sources === false &&
                                current.searchKeys.length &&
                                !current.searchCollection
                            ) {
                                setSelectedSources(current.searchKeys);
                            }
                        }
                        if (typeof event.all_sources === "boolean") {
                            current.searchAll = event.all_sources;
                        }
                        renderSourceFilters();
                        renderScopeLine();
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
                        const firstResults =
                            !renderedSnapshot &&
                            Array.isArray(event.results) &&
                            event.results.length > 0;
                        if (firstResults) {
                            renderedSnapshot = true;
                            lastStreamRenderAt = performance.now();
                            renderView();
                            renderNotice();
                        } else {
                            scheduleStreamRender(sequence);
                        }
                        return true;
                    } else if (event.type === "done") {
                        applyPage(event);
                        current.streaming = false;
                        cancelStreamRender();
                        renderView();
                        const scope = event.scope;
                        if (scope && scope.status === "partial") {
                            const reason = scope.limit_reason === "deadline"
                                ? "search deadline reached"
                                : scope.limit_reason === "source_cap"
                                    ? "source limit reached"
                                    : "partial scope";
                            $("status").textContent =
                                `${searchStatusText()} · ${reason} ` +
                                `(${scope.completed}/${scope.requested})`;
                        } else {
                            $("status").textContent = searchStatusText();
                        }
                        announceSearchStatus();
                        renderNotice();
                    } else if (event.type === "error") {
                        busy = Boolean(event.busy);
                        $("status").textContent = event.error || t("Search failed");
                        announceSearchStatus($("status").textContent);
                    }
                    return false;
                };
                while (true) {
                    const part = await reader.read();
                    if (part.done) break;
                    // Any chunk proves the stream is alive; restart the idle
                    // timer rather than counting from the request start.
                    if (onPart) onPart();
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

            function defaultBrowseMode() {
                return availableBrowseModes.has("diverse") ? "diverse" : "rare";
            }

            function renderSortControls() {
                const select = $("sortSelect");
                if (!select) return;
                $("sortControls").hidden = !current?.pageItems?.length;
                const typedQuestion = $("question").value.trim();
                // An empty box is a pending edit, not a new empty-query browse.
                // Keep showing the mode that produced the results still on
                // screen until a search actually starts for the new scope.
                const activeQuestion = typedQuestion || current?.question || "";
                for (const option of select.options) {
                    option.hidden = option.value === "relevance"
                        ? !activeQuestion : !availableBrowseModes.has(option.value);
                    option.disabled = option.hidden;
                }
                const startingSearchFromBrowse = typedQuestion && current?.question === "";
                select.value = startingSearchFromBrowse
                    ? "relevance"
                    : current?.browse || (activeQuestion ? "relevance" : defaultBrowseMode());
            }

            function setSettingsOpen(open) {
                const settings = $("settingsDetails");
                if (settings) settings.open = Boolean(open);
            }

            async function httpError(response) {
                let detail = `HTTP ${response.status}`;
                try {
                    const payload = await response.json();
                    if (Array.isArray(payload?.detail)) {
                        // FastAPI validation errors are structured objects;
                        // String(detail) would only yield "[object Object]".
                        detail = payload.detail
                            .map((item) => item?.msg || String(item))
                            .filter(Boolean)
                            .join("; ") || detail;
                    } else if (payload && payload.detail) {
                        detail = String(payload.detail);
                    }
                } catch (_) {
                    // The response was not JSON; keep the status message.
                }
                const error = new Error(detail);
                error.status = response.status;
                return error;
            }

            async function runSearch(
                question,
                selectedSources = [],
                page = 0,
                scopeSources,
                collectionKey = null,
                browse = null,
                seed = 0,
                { preserveSourceFilterOther = false } = {},
            ) {
                question = question.trim();
                if (!question && !browse) browse = defaultBrowseMode();
                if (browse !== "random") seed = 0;
                setSettingsOpen(false);
                sourceFilterShowAll = false;
                // Selecting a source mid-search re-runs the search, but the
                // expanded Other disclosure belongs to the reader's filter
                // browsing, not to the query, so keep it open.
                if (!preserveSourceFilterOther) sourceFilterShowOther = false;
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
                selectedCollectionKey = collectionKey || null;
                const discovery = scopeKeys.length || selectedCollectionKey
                    ? sourceDiscovery(question, current, browse, seed)
                    : null;
                current = {
                    question,
                    browse,
                    seed,
                    terms: queryTerms(question),
                    discovery,
                    outcomes: new Map(),
                    counts: {},
                    pageItems: [],
                    matchedBy: null,
                    total: 0,
                    pageCount: 1,
                    completed: 0,
                    sourcesTotal: 0,
                    streaming: true,
                    page: pageIndex,
                    searchKeys: scopeKeys,
                    scopeKeys,
                    searchCollection: selectedCollectionKey,
                    searchAll: !selectedCollectionKey && scopeKeys.length === 0,
                };
                announceSearchStatus();
                document.body.classList.add("search-active");
                renderSortControls();
                window.scrollTo({ top: 0, left: 0, behavior: "auto" });
                $("searchSummary").hidden = false;
                $("searchButton").disabled = true;
                $("status").innerHTML =
                    !question
                        ? `${escapeHtml(t(BROWSE_LABELS[browse]))}…`
                        : `${t("Searching for")} <span class="query-used">"${escapeHtml(question)}"</span>…`;
                $("notice").textContent = "";
                $("pager").innerHTML = "";
                renderScopeLine();
                renderSourceFilters();
                renderResults([]);
                try {
                    const selected = activeSources();
                    if (!selected.length) {
                        $("status").textContent = t(
                            "No sources are available to search.",
                        );
                        announceSearchStatus($("status").textContent);
                        return;
                    }
                    const params = new URLSearchParams({
                        q: question,
                        ...(browse ? {sort: browse} : {}),
                        ...(browse === "random" ? {seed: String(seed)} : {}),
                        limit: String(PAGE_SIZE),
                        offset: String(pageIndex * PAGE_SIZE),
                    });
                    searchScopeParams(
                        params,
                        scopeKeys,
                        selectedCollectionKey,
                    );
                    dedupeParams(params, "source");
                    // An explicit source/collection selection defines the scope
                    // directly; it is never promoted to a full search. Page size
                    // and offset are not cached keys.
                    let busyRetries = 0;
                    while (true) {
                        const streamController = new AbortController();
                        const abortStream = () => streamController.abort();
                        let streamTimer = null;
                        const armStreamTimeout = (delay) => {
                            clearTimeout(streamTimer);
                            streamTimer = setTimeout(abortStream, delay);
                        };
                        armStreamTimeout(STREAM_FIRST_BYTE_TIMEOUT);
                        const streamDeadline = setTimeout(abortStream, STREAM_TOTAL_TIMEOUT);
                        controller.signal.addEventListener("abort", abortStream, { once: true });
                        let busy;
                        try {
                            const response = await fetch(appPath("/search/stream") + "?" + params, {
                                signal: streamController.signal,
                            });
                            if (!response.ok) {
                                const error = await httpError(response);
                                try {
                                    await response.body?.cancel();
                                } catch (_) {
                                    // The body was already closed.
                                }
                                throw error;
                            }
                            busy = await consumeStream(response, sequence, () =>
                                armStreamTimeout(STREAM_IDLE_TIMEOUT),
                            );
                        } finally {
                            clearTimeout(streamTimer);
                            clearTimeout(streamDeadline);
                            controller.signal.removeEventListener("abort", abortStream);
                        }
                        if (!busy || busyRetries >= BUSY_RETRY_LIMIT || sequence !== searchSequence) break;
                        busyRetries++;
                        const delay = Math.min(
                            BUSY_RETRY_BASE_DELAY * 2 ** (busyRetries - 1),
                            BUSY_RETRY_MAX_DELAY,
                        );
                        $("status").textContent = retryStatusText(503, delay);
                        await waitForRetry(delay, controller.signal);
                    }
                } catch (error) {
                    // A gateway 503/504 on the stream usually means the request
                    // never got its first byte (for example, while waiting for
                    // an identical in-flight search). The JSON endpoint retries
                    // those, so use the same path instead of a hard failure.
                    const retryableStatus = error.status === 503 || error.status === 504;
                    if (
                        (error.name === "AbortError" || retryableStatus) &&
                        !controller.signal.aborted &&
                        sequence === searchSequence
                    ) {
                        // Some mobile browsers and proxies do not expose a timely
                        // readable body for chunked NDJSON. The JSON endpoint avoids
                        // that transport without changing the search result.
                        current.streaming = false;
                        await loadPage(pageIndex);
                    } else if (
                        error.name !== "AbortError" &&
                        !controller.signal.aborted &&
                        sequence === searchSequence
                    ) {
                        clearPartialResults();
                        $("status").textContent = t("Search failed");
                        if (error.status === 400) {
                            $("notice").textContent =
                                !question
                                    ? error.message
                                    : `${t("One or more selected sources are not available.")}` +
                                      (error.message ? ` (${error.message})` : "");
                        } else if (error.status === 422) {
                            // A validation error is usually a bad URL or a
                            // stale cross-version link; show the reason rather
                            // than only "Search failed".
                            $("notice").textContent = error.message;
                        }
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
                // Paging reuses the server's cached pool for the current search
                // scope and returns a single page. No reload, no re-stream.
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
                    ...(current.browse ? {sort: current.browse} : {}),
                    ...(current.browse === "random" ? {seed: String(current.seed)} : {}),
                    limit: String(PAGE_SIZE),
                    offset: String(pageIndex * PAGE_SIZE),
                });
                searchScopeParams(
                    params,
                    current.searchAll
                        ? []
                        : current.searchCollection
                          ? current.scopeKeys || []
                          : current.searchKeys || [],
                    current.searchCollection,
                );
                dedupeParams(params, "source");
                $("status").textContent = t("Loading page…");
                try {
                    let response;
                    let busyRetries = 0;
                    while (true) {
                        response = await fetch(appPath("/search") + "?" + params, {
                            signal: controller.signal,
                        });
                        if (response.ok) break;
                        // 503 is Zimantic's "too many searches are running" reply;
                        // 504 is a gateway giving up on a cold search. Both
                        // usually succeed once the in-flight search finishes,
                        // so retry instead of showing a hard failure.
                        if (
                            (response.status === 503 || response.status === 504) &&
                            busyRetries < BUSY_RETRY_LIMIT &&
                            sequence === searchSequence
                        ) {
                            // Release the discarded response so the connection
                            // does not stay pinned until garbage collection.
                            try {
                                await response.body?.cancel();
                            } catch (_) {
                                // The body was already closed.
                            }
                            const retryAfter = Number(response.headers.get("Retry-After"));
                            const delay = Math.min(
                                retryAfter > 0
                                    ? retryAfter * 1000
                                    : BUSY_RETRY_BASE_DELAY * 2 ** busyRetries,
                                BUSY_RETRY_MAX_DELAY,
                            );
                            busyRetries++;
                            $("status").textContent = retryStatusText(
                                response.status,
                                delay,
                            );
                            await waitForRetry(delay, controller.signal);
                            continue;
                        }
                        throw await httpError(response);
                    }
                    const items = await response.json();
                    if (sequence !== searchSequence || controller !== pageController) return;
                    current.page = pageIndex;
                    current.pageItems = items;
                    current.total = Number(response.headers.get("X-Total-Count") || items.length);
                    current.pageCount =
                        Number(response.headers.get("X-Page-Count")) || 1;
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
                        clearPartialResults();
                        $("status").textContent = t("Search failed");
                        if (error.status === 400) {
                            $("notice").textContent =
                                !current.question
                                    ? error.message
                                    : `${t("One or more selected sources are not available.")}` +
                                      (error.message ? ` (${error.message})` : "");
                        }
                        announceSearchStatus($("status").textContent);
                    }
                } finally {
                    if (controller === pageController) pageController = null;
                }
            }

            function applySourceSelection(
                question,
                selectedSources,
                page = 0,
                scopeSources,
                collectionKey = null,
                browse = current?.browse || null,
                seed = current?.seed || 0,
                options = {},
            ) {
                const requested = [...new Set(selectionValues(selectedSources))];
                const scopeKeys = [...new Set(selectionValues(
                    scopeSources === undefined ? requested : scopeSources,
                ))];
                if (
                    current &&
                    current.question === question &&
                    (current.browse || null) === browse &&
                    (current.seed || 0) === seed &&
                    (current.searchCollection || null) === (collectionKey || null)
                ) {
                    const previous = current.searchAll
                        ? []
                        : collectionKey
                          ? current.scopeKeys || []
                          : current.searchKeys || [];
                    if (
                        previous.length === scopeKeys.length &&
                        previous.every((key) => scopeKeys.includes(key))
                    ) {
                        setSelectedSources(requested);
                        loadPage(page);
                        return;
                    }
                }
                runSearch(
                    question,
                    requested,
                    page,
                    scopeKeys,
                    collectionKey,
                    browse,
                    seed,
                    options,
                );
            }

            function makeStateUrl(
                question,
                selectedSources,
                page,
                modal = null,
                scopeSources,
                collectionKey,
                browse,
                seed,
            ) {
                // Keep search and modal state in the query string so links are shareable
                // and Back/Forward just work.
                const url = new URL(location.href);
                const resolvedBrowse = browse === undefined
                    ? current?.browse || url.searchParams.get("sort")
                    : browse;
                const resolvedSeed = seed ?? current?.seed ?? url.searchParams.get("seed") ?? 0;
                const hasSearch = Boolean(question || resolvedBrowse || current || url.searchParams.has("q"));
                url.hash = "";
                url.search = "";
                if (hasSearch) url.searchParams.set("q", question);
                if (resolvedBrowse) {
                    url.searchParams.set("sort", resolvedBrowse);
                    if (resolvedBrowse === "random") {
                        url.searchParams.set("seed", String(resolvedSeed));
                    }
                }
                const sourceValues = [...new Set(selectionValues(selectedSources))];
                const hasCollectionArg = collectionKey !== undefined;
                const explicitScope = scopeSources !== undefined || hasCollectionArg;
                const sameSearch =
                    explicitScope || (current && current.question === question);
                const resolvedCollection = hasCollectionArg
                    ? collectionKey
                    : sameSearch && current
                      ? current.searchCollection || null
                      : null;
                if (resolvedCollection) {
                    // A collection is one canonical scope selector, never a
                    // list of hundreds of source keys.
                    url.searchParams.set("collection", resolvedCollection);
                }
                sourceValues.forEach((source) =>
                    url.searchParams.append("source", source),
                );
                if (page > 0) url.searchParams.set("page", String(page + 1));
                if (modal) url.searchParams.set("modal", modal);
                // Search state owns q/source/collection/page/modal, but a
                // library view also has its filters in the URL. Keep them when
                // opening an article or another history entry from /library.
                if (
                    typeof appPath === "function" &&
                    location.pathname === appPath("/library")
                ) {
                    const filter =
                        ($("libraryFilter")?.value || "").trim() ||
                        readLibraryFilter();
                    if (filter) url.searchParams.set("filter", filter);
                    const provider = $("libraryProviderFilter")?.value || "";
                    const media = $("libraryMediaFilter")?.value || "";
                    const language = $("libraryLanguageFilter")?.value || "";
                    if (provider) url.searchParams.set("provider", provider);
                    if (media) url.searchParams.set("media", media);
                    if (language) url.searchParams.set("language", language);
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
                replaceQueryParam("modal", value);
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
                replaceQueryParam(
                    "page",
                    canonical && canonical !== "1" ? canonical : null,
                );
            }

            function normalizeSortParam() {
                // Remove unknown sort values and an explicit relevance before
                // the first search, so the address bar describes the request
                // the server actually receives. A valid browse sort is left
                // for loadConfig's availability check.
                const params = new URLSearchParams(location.search);
                if (!params.has("sort")) return;
                const raw = params.get("sort");
                if (raw === "relevance") {
                    replaceQueryParam("sort", null);
                    return;
                }
                if (!raw || !BROWSE_MODES.has(raw)) {
                    replaceQueryParam("sort", null);
                }
            }

            function readLocationState() {
                const params = new URLSearchParams(location.search);
                const pageNumber = Number.parseInt(params.get("page") || "1", 10);
                const sourceValues = [
                    ...new Set(params.getAll("source").filter(Boolean)),
                ];
                const collection = params.get("collection") || null;
                const scopeSources = sourceValues;
                const question = (params.get("q") || "").trim().slice(0, MAX_QUERY_LENGTH);
                const requestedSort = params.get("sort");
                // A hand-edited or stale URL must not turn into a blank sort
                // control and a server 422. Unknown values fall back to
                // relevance (or the empty-query default), exactly like an
                // omitted sort.
                const sortMode = requestedSort
                    && requestedSort !== "relevance"
                    && BROWSE_MODES.has(requestedSort)
                    ? requestedSort
                    : null;
                const defaultSort = !question && params.has("q") && !sortMode;
                const browse = defaultSort ? defaultBrowseMode() : sortMode;
                return {
                    question,
                    browse,
                    defaultSort,
                    seed: browse === "random" &&
                        /^\d+$/.test(params.get("seed") || "") &&
                        Number(params.get("seed")) < 2 ** 32
                        ? Number(params.get("seed")) : 0,
                    selectedSources: scopeSources,
                    scopeSources,
                    collection,
                    hasScope: scopeSources.length > 0 || Boolean(collection),
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
                const button = event.target.closest(
                    "button[data-source], button[data-source-action], button[data-source-collection]",
                );
                if (!button || button.disabled) return;
                const key = button.dataset.source || null;
                const collectionKey = button.dataset.sourceCollection || null;
                const focusButton = (wanted) => {
                    const buttons = [
                        ...$("sourceFilterOptions").querySelectorAll("button"),
                    ];
                    const next =
                        (wanted &&
                            buttons.find(
                                (item) =>
                                    sourceFilterControlKey(item) === wanted,
                            )) ||
                        buttons.find((item) => !item.disabled);
                    if (next) next.focus({ preventScroll: true });
                    else $("question").focus();
                };
                if (!current) {
                    if (collectionKey) {
                        selectedCollectionKey =
                            selectedCollectionKey === collectionKey
                                ? null
                                : collectionKey;
                        selectedSourceKeys.clear();
                    } else if (key && selectedSourceKeys.has(key)) {
                        selectedSourceKeys.delete(key);
                    } else if (key) {
                        selectedCollectionKey = null;
                        selectedSourceKeys.add(key);
                    }
                    const selected = [...selectedSourceKeys];
                    history.replaceState(
                        history.state,
                        "",
                        makeStateUrl(
                            "",
                            selected,
                            0,
                            null,
                            selectedCollectionKey ? [] : selected,
                            selectedCollectionKey,
                        ),
                    );
                    renderSourceSelectionSummary();
                    renderScopeLine();
                    renderSortControls();
                    focusButton(
                        collectionKey
                            ? `collection:${collectionKey}`
                            : key
                              ? `source:${key}`
                              : "",
                    );
                    return;
                }
                if (collectionKey) {
                    // One collection group is selected as a unit. Switching
                    // groups replaces the previous selection.
                    selectedCollectionKey =
                        selectedCollectionKey === collectionKey
                            ? null
                            : collectionKey;
                    selectedSourceKeys.clear();
                } else if (button.dataset.sourceAction === "more") {
                    // Reveal the remaining matching source chips without
                    // changing the search scope or the current selection.
                    const hidden = sourceFilterLayout().otherOutcomes.map(
                        (outcome) => outcome.source.key,
                    );
                    sourceFilterShowAll = true;
                    renderSourceFilters();
                    focusButton(hidden.length ? `source:${hidden[0]}` : "");
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
                    focusButton(
                        hidden.length ? `source:${hidden[0].key}` : "",
                    );
                    return;
                } else if (selectedCollectionKey) {
                    // Choosing an individual source leaves group mode.
                    selectedCollectionKey = null;
                    selectedSourceKeys = new Set(key ? [key] : []);
                } else if (selectedSourceKeys.has(key)) {
                    selectedSourceKeys.delete(key);
                } else if (key) {
                    selectedSourceKeys.add(key);
                }
                const selected = [...selectedSourceKeys];
                commitAutoSearchHistory();
                const nextScope = nextSearchScope(selected);
                history.pushState(
                    null,
                    "",
                    makeStateUrl(
                        current.question,
                        selected,
                        0,
                        null,
                        nextScope,
                        selectedCollectionKey,
                    ),
                );
                renderSourceFilters();
                applySourceSelection(
                    current.question,
                    selected,
                    0,
                    nextScope,
                    selectedCollectionKey,
                    current?.browse || null,
                    current?.seed || 0,
                    { preserveSourceFilterOther: !collectionKey },
                );
            });

            $("results").addEventListener("click", (event) => {
                const target = event.target instanceof Element ? event.target : null;
                const link = target?.closest(".result-title a[href], a.result-thumb[href]");
                if (!link) {
                    const desktopSnippet =
                        !matchMedia("(max-width: 480px)").matches &&
                        Boolean(target?.closest(".result-snippet"));
                    if (
                        desktopSnippet ||
                        modifiedClick(event) ||
                        target?.closest("a, button, input, select, textarea, details, [contenteditable]") ||
                        window.getSelection()?.toString()
                    ) {
                        return;
                    }
                    target?.closest(".result")?.querySelector(".result-title a[href]")?.click();
                    return;
                }
                if (modifiedClick(event)) return;
                if (new URL(link.href, location.href).origin !== location.origin) return;
                if (!isHtmlMimetype(link.dataset.mimetype)) return;
                event.preventDefault();
                openArticleModal(link);
            });

            $("libraryGrid").addEventListener("click", (event) => {
                const target = event.target instanceof Element ? event.target : null;
                const searchCollection = target?.closest(
                    "button[data-library-search-collection]",
                );
                if (searchCollection) {
                    const collection = collectionByKey(
                        searchCollection.dataset.librarySearchCollection,
                    );
                    if (collection) searchWithinLibraryCollection(collection);
                    return;
                }
                const more = target?.closest("button[data-library-more-collection]");
                if (more) {
                    loadCollectionSources(more.dataset.libraryMoreCollection, {
                        append: true,
                        filters: currentLibraryFilters(),
                        sequence: libraryLoadSequence,
                    });
                    return;
                }
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
                trapModalTabFocus($("libraryInfoModal"), event);
            });
            $("libraryFilter").addEventListener("input", scheduleLibraryReload);
            $("libraryProviderFilter").addEventListener("change", scheduleLibraryReload);
            $("libraryMediaFilter").addEventListener("change", scheduleLibraryReload);
            $("libraryLanguageFilter").addEventListener("change", scheduleLibraryReload);
            $("libraryScopeLineReset").addEventListener("click", resetLibraryFilters);
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

            $("scopeLineReset").addEventListener("click", () => {
                if (!current) {
                    setSelectedSources([]);
                    selectedCollectionKey = null;
                    sourceFilterShowAll = false;
                    sourceFilterShowOther = false;
                    history.replaceState(
                        history.state,
                        "",
                        makeStateUrl("", [], 0, null, [], null),
                    );
                    renderSourceSelectionSummary();
                    renderScopeLine();
                    renderSortControls();
                    $("question").focus();
                    return;
                }
                if (current.searchAll) return;
                commitAutoSearchHistory();
                setSelectedSources([]);
                selectedCollectionKey = null;
                history.pushState(
                    null,
                    "",
                    makeStateUrl(current.question, [], 0, null, [], null),
                );
                applySourceSelection(current.question, [], 0, [], null);
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
                trapModalTabFocus($("externalLinkNotice"), event);
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
                selectedCollectionKey = null;
                resultList = null;
                announceSearchStatus();
                document.body.classList.remove("search-active");
                renderSortControls();
                setSettingsOpen(true);
                $("searchSummary").hidden = true;
                $("sourceFilterToolbar").hidden = true;
                $("status").textContent = "";
                renderMatchedBy();
                $("sourceFilters").hidden = true;
                $("sourceFilterOptions").replaceChildren();
                $("sourceFilterStatus").textContent = "";
                $("sourceFilterSearch").value = "";
                $("scopeLine").hidden = true;
                $("scopeLineText").textContent = "";
                $("filterLine").innerHTML = "";
                $("notice").textContent = "";
                $("pager").innerHTML = "";
                const empty = document.createElement("div");
                empty.className = "empty";
                $("results").replaceChildren(empty);
                $("searchButton").disabled = false;
            }

            function clearSearch(pushHistory) {
                // The heading clears the current search; submitting an empty field
                // searches the selected scope instead.
                const hadSearch = current !== null || Boolean(location.search);
                $("question").value = "";
                resetResults();
                if (hadSearch) {
                    history[pushHistory ? "pushState" : "replaceState"](null, "", location.pathname);
                }
                $("question").focus();
                window.scrollTo({ top: 0, left: 0, behavior: "auto" });
            }

            function toggleHeadingSearch() {
                if (location.pathname === appPath("/library")) {
                    setSettingsOpen(true);
                    history.pushState(null, "", appPath("/"));
                    initialize();
                    return;
                }
                if (current) {
                    previousHeadingSearch = {
                        question: current.question,
                        browse: current.browse || null,
                        seed: current.seed || 0,
                        selectedSources: [...selectedSourceKeys],
                        scopeSources:
                            current.searchAll
                                ? []
                                : current.searchCollection
                                  ? current.scopeKeys || []
                                  : [...new Set(current.searchKeys || [])],
                        collection: current.searchCollection || null,
                        page: current.page,
                    };
                    clearSearch(true);
                    return;
                }
                if (!previousHeadingSearch) {
                    setSettingsOpen(true);
                    return;
                }
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
                        previous.collection,
                        previous.browse,
                        previous.seed,
                    ),
                );
                runSearch(
                    previous.question,
                    previous.selectedSources,
                    previous.page,
                    previous.scopeSources,
                    previous.collection,
                    previous.browse,
                    previous.seed,
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
                if (event && modifiedClick(event)) {
                    window.open(appPath("/"), "_blank", "noopener");
                    return;
                }
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
                if (question.length > MAX_QUERY_LENGTH) {
                    $("searchButton").disabled = false;
                    $("status").textContent = "";
                    $("notice").textContent =
                        `Search queries are limited to ${MAX_QUERY_LENGTH.toLocaleString()} characters.`;
                    return;
                }
                // Keep the current source or collection filter when the query
                // changes, so a search started from a library card stays scoped.
                // Discovery chips belong to the old query; a new query searches
                // only the selected sources instead of repeating broad fan-out.
                const selectedSources = [...selectedSourceKeys];
                const hasCollection = Boolean(selectedCollectionKey);
                const scopeSources = nextSearchScope(selectedSources);
                const selectedSort = $("sortSelect").value;
                const browse = selectedSort && selectedSort !== "relevance"
                    ? selectedSort : question ? null : defaultBrowseMode();
                const seed = browse === "random"
                    ? Math.floor(Math.random() * 2 ** 32) : 0;
                const url = makeStateUrl(
                    question,
                    selectedSources,
                    0,
                    null,
                    scopeSources,
                    hasCollection ? selectedCollectionKey : undefined,
                    browse,
                    seed,
                );
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
                runSearch(
                    question,
                    selectedSources,
                    0,
                    scopeSources,
                    hasCollection ? selectedCollectionKey : null,
                    browse,
                    seed,
                );
            }

            $("question").addEventListener("input", (event) => {
                if (composing || event.isComposing) return;
                renderSortControls();
                scheduleAutoSubmit();
            });
            $("question").addEventListener("compositionstart", () => {
                composing = true;
                clearTimeout(autoSubmitTimer);
                autoSubmitTimer = null;
            });
            $("question").addEventListener("compositionend", () => {
                composing = false;
                renderSortControls();
                scheduleAutoSubmit();
            });

            $("searchForm").addEventListener("submit", (event) => {
                event.preventDefault();
                clearTimeout(autoSubmitTimer);
                autoSubmitTimer = null;
                if (composing) return;
                submitSearch(true);
            });

            $("sortSelect").addEventListener("change", () => {
                clearTimeout(autoSubmitTimer);
                autoSubmitTimer = null;
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
                    const params = new URLSearchParams(location.search);
                    const filter = readLibraryFilter();
                    const provider = params.get("provider") || "";
                    const media = params.get("media") || "";
                    const language = params.get("language") || "";
                    const changed =
                        $("libraryFilter").value !== filter ||
                        $("libraryProviderFilter").value !== provider ||
                        $("libraryMediaFilter").value !== media ||
                        $("libraryLanguageFilter").value !== language;
                    $("libraryFilter").value = filter;
                    $("libraryProviderFilter").value = provider;
                    $("libraryMediaFilter").value = media;
                    setLibraryLanguageFilter(language);
                    if (changed) loadLibraryData();
                }
                const state = readLocationState();
                syncArticleModal(state.modal);
                if (!$("articleModal").hidden) {
                    closeLibraryInfo({ navigateHistory: false });
                } else {
                    syncLibraryInfo(history.state?.zimanticLibraryInfo || null);
                }
                if (!state.question && !state.browse) {
                    // Navigated back/forward to the no-search entry: match it.
                    $("question").value = "";
                    resetResults();
                    return;
                }
                $("question").value = state.question;
                const currentScope = current && !current.searchAll
                    ? [...new Set(
                          current.searchCollection
                              ? current.scopeKeys || []
                              : current.searchKeys || [],
                      )]
                    : [];
                const stateScope = state.hasScope ? state.scopeSources : [];
                const sameScope =
                    currentScope.length === stateScope.length &&
                    currentScope.every((key) => stateScope.includes(key));
                const sameCollection =
                    (current?.searchCollection || null) === state.collection;
                if (
                    current &&
                    current.question === state.question &&
                    (current.browse || null) === (state.browse || null) &&
                    (current.seed || 0) === (state.seed || 0) &&
                    sameCollection &&
                    sameScope
                ) {
                    // Same query and search scope: reuse the pool when the
                    // displayed filters change; otherwise the scope itself
                    // changed and needs a new search.
                    applySourceSelection(
                        state.question,
                        state.selectedSources,
                        state.page,
                        undefined,
                        state.collection,
                        state.browse,
                        state.seed,
                    );
                } else {
                    runSearch(
                        state.question,
                        state.selectedSources,
                        state.page,
                        state.scopeSources,
                        state.collection,
                        state.browse,
                        state.seed,
                    );
                }
            });

            function applyDiscoveryData(payload, libraryView) {
                if (!Array.isArray(payload) || !payload.every(isCanonicalSourceRecord)) return;
                if (libraryView) {
                    // Unreadable ZIMs stay visible so the card can explain why
                    // they cannot be opened. Readable archives without a
                    // homepage are also kept; the card can offer a random
                    // article when the source is searchable.
                    librarySources = sortLibrarySources(payload);
                    refreshLibraryLanguageOptions();
                    renderLibrary();
                } else {
                    sources = payload;
                    renderSourceSelectionSummary();
                }
            }

            async function mapWithConcurrency(values, limit, worker) {
                const queue = [...values];
                const runners = Array.from(
                    { length: Math.min(Math.max(1, limit), queue.length) },
                    async () => {
                        while (queue.length) {
                            const value = queue.shift();
                            await worker(value);
                        }
                    },
                );
                await Promise.all(runners);
            }

            function currentLibraryFilters() {
                return {
                    provider: $("libraryProviderFilter")?.value || "",
                    media: $("libraryMediaFilter")?.value || "",
                    language: $("libraryLanguageFilter")?.value || "",
                    q: libraryFilterTerms(),
                };
            }

            function refreshLibraryLanguageOptions() {
                for (const source of librarySources) {
                    for (const language of libraryLanguageCodes(source)) {
                        libraryLanguageOptions.add(language);
                    }
                }
                renderLibraryLanguageOptions();
            }

            function refreshLibrarySourceList() {
                librarySources = sortLibrarySources(
                    [...libraryCollectionPages.values()].flatMap(
                        (state) => state.sources,
                    ),
                );
                refreshLibraryLanguageOptions();
            }

            function restoreLibraryCache() {
                const cachedCollections = readCollectionCache();
                if (!cachedCollections) return;
                collections = cachedCollections;
                libraryCollections = [...cachedCollections];
                const known = new Set(cachedCollections.map((record) => record.key));
                for (const page of readJsonCache(LIBRARY_PAGE_CACHE_KEY) || []) {
                    if (
                        !page ||
                        !known.has(page.key) ||
                        !Array.isArray(page.sources) ||
                        !page.sources.every(
                            (source) =>
                                isCanonicalSourceRecord(source) &&
                                collectionKeyFor(source) === page.key,
                        ) ||
                        !Number.isInteger(page.total) ||
                        page.total < page.sources.length ||
                        typeof page.hasMore !== "boolean" ||
                        typeof page.version !== "string"
                    ) {
                        continue;
                    }
                    libraryCollectionPages.set(page.key, {
                        sources: page.sources,
                        total: page.total,
                        hasMore: page.hasMore,
                        version: page.version,
                        loading: false,
                        error: null,
                    });
                }
                refreshLibrarySourceList();
                renderLibrary();
            }

            async function loadCollectionSources(
                key,
                { append = false, filters = currentLibraryFilters(), sequence = 0, retry = false } = {},
            ) {
                if (!key) return;
                const collection = collectionByKey(key);
                const previous = libraryCollectionPages.get(key);
                if (append && previous?.loading) return;
                const offset = append ? previous?.sources.length || 0 : 0;
                const state = {
                    ...(previous || {}),
                    collection,
                    sources: previous?.sources || [],
                    total: previous?.total || 0,
                    hasMore: previous?.hasMore || false,
                    version: previous?.version || "",
                    loading: true,
                    error: null,
                };
                libraryCollectionPages.set(key, state);
                if (append) renderLibrary();
                try {
                    const result = await fetchJsonPage(appPath("/library.json"), {
                        limit: Math.min(COLLECTION_SOURCE_LIMIT, 1000),
                        offset,
                        collection: key,
                        ...filters,
                    });
                    if (sequence && sequence !== libraryLoadSequence) return;
                    if (
                        append &&
                        previous?.version &&
                        result.version &&
                        previous.version !== result.version &&
                        !retry
                    ) {
                        // The discovery revision moved while this group was
                        // loading. Restart the group; never splice pages from
                        // two different snapshots.
                        return loadCollectionSources(key, {
                            append: false,
                            filters,
                            sequence,
                            retry: true,
                        });
                    }
                    if (!result.items.every(isCanonicalSourceRecord)) {
                        throw new Error("invalid source payload");
                    }
                    const merged =
                        append &&
                        previous?.version &&
                        result.version &&
                        previous.version === result.version
                            ? [...previous.sources, ...result.items]
                            : result.items;
                    const unique = new Map();
                    for (const source of merged) unique.set(source.key, source);
                    libraryCollectionPages.set(key, {
                        collection,
                        sources: [...unique.values()],
                        total: result.total,
                        hasMore: result.hasMore,
                        version: result.version || previous?.version || "",
                        loading: false,
                        error: null,
                    });
                } catch (error) {
                    if (sequence && sequence !== libraryLoadSequence) return;
                    libraryCollectionPages.set(key, {
                        ...state,
                        sources: append ? previous?.sources || [] : state.sources,
                        loading: false,
                        error: error.message || "load failed",
                    });
                }
                refreshLibrarySourceList();
                renderLibrary();
            }

            async function loadLibraryData({ reset = true } = {}) {
                const sequence = ++libraryLoadSequence;
                const filters = currentLibraryFilters();
                const filtered = Boolean(
                    filters.provider || filters.media || filters.language || filters.q,
                );
                if (reset) {
                    libraryCollectionPages.clear();
                    librarySources = [];
                    if (!filtered) restoreLibraryCache();
                }
                try {
                    const records = await fetchPagedJson(appPath("/collections.json"), {
                        ...filters,
                        limit: Math.max(50, PAGE_SIZE),
                    });
                    if (sequence !== libraryLoadSequence) return;
                    if (!records.every(isCanonicalCollectionRecord)) {
                        throw new Error("invalid collection payload");
                    }
                    collections = records;
                    libraryCollections = [...records];
                    const known = new Set(records.map((record) => record.key));
                    for (const key of libraryCollectionPages.keys()) {
                        if (!known.has(key)) libraryCollectionPages.delete(key);
                    }
                    refreshLibrarySourceList();
                    if (!filtered) writeJsonCache(COLLECTION_CACHE_KEY, records);
                    renderLibrary();
                    await mapWithConcurrency(
                        libraryCollections.map((collection) => collection.key),
                        4,
                        (key) =>
                            loadCollectionSources(key, {
                                append: false,
                                filters,
                                sequence,
                            }),
                    );
                    if (!libraryCollections.length) {
                        const fallback = await fetchPagedJson(appPath("/library.json"), {
                            ...filters,
                            limit: Math.min(COLLECTION_SOURCE_LIMIT, 1000),
                        });
                        if (sequence !== libraryLoadSequence) return;
                        if (!fallback.every(isCanonicalSourceRecord)) {
                            throw new Error("invalid source payload");
                        }
                        if (!filtered) writeJsonCache(LIBRARY_CACHE_KEY, fallback);
                        librarySources = sortLibrarySources(fallback);
                        refreshLibraryLanguageOptions();
                        renderLibrary();
                    }
                    if (sequence === libraryLoadSequence && !filtered) {
                        writeJsonCache(
                            LIBRARY_PAGE_CACHE_KEY,
                            [...libraryCollectionPages.entries()]
                                .filter(([, page]) => !page.error || page.sources.length)
                                .map(([key, page]) => ({
                                    key,
                                    sources: page.sources,
                                    total: page.total,
                                    hasMore: page.hasMore,
                                    version: page.version,
                                })),
                        );
                    }
                } catch (error) {
                    if (sequence !== libraryLoadSequence) return;
                    renderLibrary();
                    $("libraryStatus").textContent =
                        `${t("Could not load the library")}: ${error.message}`;
                }
            }

            async function loadSearchData() {
                collections = readCollectionCache() || [];
                const cached = readCanonicalCache(SOURCE_CACHE_KEY);
                if (cached) applyDiscoveryData(cached, false);
                renderSourceSelectionSummary();
                renderScopeLine();
                await Promise.all([
                    fetchCachedJson(appPath("/sources"), SOURCE_CACHE_KEY, {
                        limit: 200,
                    }).then((data) => {
                        applyDiscoveryData(data.payload, false);
                        renderScopeLine();
                        if (data.stale) {
                            $("status").textContent =
                                `${t("Using cached source data; refresh failed")}: ${data.error.message}`;
                        }
                    }),
                    fetchPagedJson(appPath("/collections.json"), { limit: 200 })
                        .then((records) => {
                            if (!records.every(isCanonicalCollectionRecord)) return;
                            collections = records;
                            writeJsonCache(COLLECTION_CACHE_KEY, records);
                            renderSourceSelectionSummary();
                            renderScopeLine();
                        })
                        .catch(() => {
                            // Collection labels are optional; cached labels or
                            // canonical keys still identify the search scope.
                        }),
                ]);
            }

            async function loadConfig() {
                const configResponse = await fetch(appPath("/config"), {
                    cache: "no-cache",
                });
                if (!configResponse.ok) return;
                const config = await configResponse.json();
                setAvailableLibraryFilterOptions(config.library_filters);
                debugMode = Boolean(config.debug);
                if (Array.isArray(config.browse_modes)) {
                    availableBrowseModes = new Set(config.browse_modes);
                }
                renderSortControls();
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
                    Number.isInteger(config.collection_source_limit) &&
                    config.collection_source_limit > 0
                ) {
                    COLLECTION_SOURCE_LIMIT = config.collection_source_limit;
                }
                if (
                    Number.isInteger(config.max_query_length) &&
                    config.max_query_length > 0
                ) {
                    MAX_QUERY_LENGTH = config.max_query_length;
                    $("question").maxLength = MAX_QUERY_LENGTH;
                }
            }

            async function initialize() {
                normalizePageParam();
                normalizeSortParam();
                const libraryView = location.pathname === appPath("/library");
                const startupStatus = $("startupStatus");
                if (libraryView) {
                    $("libraryFilter").value = readLibraryFilter();
                    const libraryParams = new URLSearchParams(location.search);
                    $("libraryProviderFilter").value =
                        libraryParams.get("provider") || "";
                    $("libraryMediaFilter").value =
                        libraryParams.get("media") || "";
                    setLibraryLanguageFilter(libraryParams.get("language") || "");
                }
                const state = readLocationState();
                if (state.browse === "random" && !new URLSearchParams(location.search).has("seed")) {
                    state.seed = Math.floor(Math.random() * 2 ** 32);
                    replaceQueryParam("seed", String(state.seed));
                }
                let invalidScope = false;
                // Apply the URL state before the discovery fetches below: a query typed
                // while they are in flight must keep the URL's source selection.
                applyLocationScope(state);
                $("question").value = state.question || "";
                const initialSearchSequence = searchSequence;
                $("homeHeading").setAttribute(
                    "aria-label",
                    libraryView
                        ? "Zimantic: switch to search"
                        : "Zimantic: clear or restore search",
                );
                // Render the requested view and focus the search box before
                // waiting on discovery. The data below is an enhancement, not
                // a prerequisite for the page to look ready.
                $("searchView").hidden = libraryView;
                $("libraryView").hidden = !libraryView;
                document.body.classList.toggle("library-active", libraryView);
                setSettingsOpen(!libraryView && !state.question && !state.browse && !state.modal);
                for (const link of document.querySelectorAll(".view-nav a")) {
                    const isLibraryLink = link.getAttribute("href") === "library";
                    if (isLibraryLink === libraryView) {
                        link.setAttribute("aria-current", "page");
                    } else {
                        link.removeAttribute("aria-current");
                    }
                }
                if (!libraryView && !state.question && !state.modal) {
                    $("question").focus();
                }
                if (!libraryView) {
                    renderSourceSelectionSummary();
                    renderScopeLine();
                    if (startupStatus) startupStatus.hidden = false;
                }
                if (libraryView) {
                    const filters = currentLibraryFilters();
                    if (
                        !filters.provider &&
                        !filters.media &&
                        !filters.language &&
                        !filters.q
                    ) {
                        restoreLibraryCache();
                    }
                }
                // Source discovery is read-only; refreshing the index/catalog set is an
                // admin action done only via `zimantic reload` or SIGHUP.
                try {
                    if (libraryView) {
                        await loadConfig();
                        await loadLibraryData({ reset: false });
                    } else {
                        await Promise.all([loadConfig(), loadSearchData()]);
                    }
                } catch (error) {
                    const target = libraryView ? $("libraryStatus") : $("status");
                    target.textContent =
                        `${t("Could not load")} ${libraryView ? t("library") : t("sources")}: ${error.message}`;
                } finally {
                    if (startupStatus) startupStatus.hidden = true;
                }
                if (state.browse && !availableBrowseModes.has(state.browse)) {
                    // A browse mode from a different server mode (for example
                    // outliers after a --fast restart) would make the server
                    // reject the search. Fall back instead of leaving a blank
                    // sort control and a generic "Search failed".
                    state.browse = null;
                    state.defaultSort =
                        !state.question &&
                        new URLSearchParams(location.search).has("q");
                    if (state.defaultSort) state.browse = defaultBrowseMode();
                    replaceQueryParam("sort", null);
                }
                if (state.defaultSort) state.browse = defaultBrowseMode();
                if (!libraryView && (sources.length || collections.length)) {
                    const knownSources = new Set(
                        sources.map((source) => source.key),
                    );
                    const knownCollections = new Set(
                        collections.map((collection) => collection.key),
                    );
                    const unknownSources = sources.length
                        ? [...selectedSourceKeys].filter(
                              (key) => !knownSources.has(key),
                          )
                        : [];
                    const unknownCollection =
                        state.collection &&
                        collections.length &&
                        !knownCollections.has(state.collection)
                            ? state.collection
                            : null;
                    const warnings = [];
                    if (unknownSources.length) {
                        const valid = [...selectedSourceKeys].filter((key) =>
                            knownSources.has(key),
                        );
                        setSelectedSources(valid);
                        warnings.push(
                            `${t("Unknown source")}: ${unknownSources.join(", ")}. ` +
                                t("Choose a source from the list."),
                        );
                    }
                    if (unknownCollection) {
                        state.collection = null;
                        selectedCollectionKey = null;
                        warnings.push(
                            `${t("Unknown collection")}: ${unknownCollection}. ` +
                                t("Choose a collection from the library."),
                        );
                    }
                    if (warnings.length) {
                        // An unknown selector is an error, never an implicit
                        // broader search. Remove only the bad parts so the
                        // next deliberate search can succeed, and keep the
                        // scope UI from continuing to show a phantom chip or
                        // collection.
                        $("notice").textContent = warnings.join(" ");
                        invalidScope = Boolean(state.question || state.browse);
                        if (invalidScope) {
                            $("status").textContent = t("Search failed");
                        }
                        const selected = [...selectedSourceKeys];
                        history.replaceState(
                            history.state,
                            "",
                            makeStateUrl(
                                state.question || "",
                                selected,
                                state.page,
                                state.modal,
                                selected,
                                state.collection,
                                state.browse,
                                state.seed,
                            ),
                        );
                        renderSourceSelectionSummary();
                        renderScopeLine();
                        renderSourceFilters();
                        renderSortControls();
                    }
                }
                syncArticleModal(state.modal);
                if (!$("articleModal").hidden) {
                    closeLibraryInfo({ navigateHistory: false });
                } else if (libraryView) {
                    syncLibraryInfo(history.state?.zimanticLibraryInfo || null);
                }
                if (
                    (state.question || state.browse) &&
                    searchSequence === initialSearchSequence &&
                    $("question").value === state.question
                ) {
                    if (!invalidScope) {
                        const knownNow = new Set(
                            (libraryView ? [] : sources).map((source) => source.key),
                        );
                        const selectedNow = [...selectedSourceKeys];
                        const scopeNow = libraryView
                            ? state.scopeSources
                            : state.scopeSources.filter((key) =>
                                  knownNow.has(key),
                              );
                        runSearch(
                            state.question,
                            selectedNow,
                            state.page,
                            scopeNow,
                            state.collection,
                            state.browse,
                            state.seed,
                        );
                    }
                } else if (!libraryView) {
                    renderSourceSelectionSummary();
                    renderScopeLine();
                    $("question").focus();
                }
            }

            $("localePicker")?.addEventListener("change", () => {
                libraryCardCache = new WeakMap();
                refreshLibraryLanguageOptions();
                if (!$("libraryView").hidden) renderLibrary();
                if (current) {
                    renderView();
                    $("status").textContent = searchStatusText();
                    renderScopeLine();
                    renderNotice();
                } else if (!$("searchView").hidden) {
                    renderSourceSelectionSummary();
                    renderScopeLine();
                }
            });

            initialize();
        })();

// Shared parent-page navigation for the article toolbar.
//
// The /content/ shell can run embedded in the search app's modal or as a
// standalone page. Both need to discover the article's parent page and the
// previous/next links listed there, so keep the URL mapping and parsing in one
// place. Callers pass their own base path (web_base_path) and fetch the
// returned candidates themselves.
(() => {
  "use strict";

  const articleRouteValue = (url) => url.pathname + url.search;

  const stripBasePath = (pathname, basePath) => {
    if (!basePath) return pathname;
    if (pathname === basePath) return "/";
    return pathname.startsWith(`${basePath}/`)
      ? pathname.slice(basePath.length)
      : pathname;
  };

  const appPath = (path, basePath) => `${basePath}${path}`;

  const articleRouteParts = (url, basePath) => {
    const parts = stripBasePath(url.pathname, basePath).split("/").filter(Boolean);
    return parts[0] === "article" && parts.length >= 3 ? parts : null;
  };

  // The modal frame loads a /content/ toolbar shell; its inner iframe loads the
  // raw entry from /article/. These helpers convert between the two so parent
  // navigation and thumbnails can read the raw article while the UI keeps using
  // the shell (toolbar-bearing) URLs.
  const rawArticleUrl = (articleUrl, basePath) => {
    try {
      const url = new URL(articleUrl?.href || String(articleUrl), location.href);
      const pathname = stripBasePath(url.pathname, basePath);
      if (url.origin !== location.origin || !pathname.startsWith("/content/")) {
        return null;
      }
      const raw = new URL(url.href);
      raw.pathname = appPath(`/article${pathname.slice("/content".length)}`, basePath);
      return raw;
    } catch (_) {
      return null;
    }
  };

  const contentUrlFromRaw = (rawUrl, basePath) => {
    try {
      const url = new URL(rawUrl, location.href);
      const pathname = stripBasePath(url.pathname, basePath);
      if (url.origin !== location.origin || !pathname.startsWith("/article/")) {
        return null;
      }
      const content = new URL(url.href);
      content.pathname = appPath(`/content${pathname.slice("/article".length)}`, basePath);
      return content;
    } catch (_) {
      return null;
    }
  };

  // Both spellings are tried because ZIMs disagree about directory entries:
  // zimit captures often have "site/page" children whose index is "site/", with
  // no "site" entry at all. Prefer the plain path so ordinary hierarchies keep
  // their existing parent.
  const parentArticleUrls = (articleUrl, basePath) => {
    const pathname = stripBasePath(articleUrl.pathname, basePath).replace(/\/+$/, "");
    const parts = pathname.split("/").filter(Boolean);
    if (parts[0] !== "content" || parts.length < 3) return [];
    const lastSlash = pathname.lastIndexOf("/");
    if (lastSlash <= 0) return [];
    const parentPath = pathname.slice(0, lastSlash);
    return [parentPath, `${parentPath}/`].map(
      (path) => new URL(appPath(path, basePath), location.origin)
    );
  };

  const readParentArticleNavigation = (html, rawParentUrl, articleUrl, basePath) => {
    const document = new DOMParser().parseFromString(html, "text/html");
    const parentParts = articleRouteParts(rawParentUrl, basePath);
    if (!parentParts) return null;
    const zim = parentParts[1];
    const parentContentUrl = contentUrlFromRaw(rawParentUrl, basePath);
    if (!parentContentUrl) return null;
    const parentValue = articleRouteValue(parentContentUrl);
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
      const linkParts = articleRouteParts(linkUrl, basePath);
      if (linkUrl.origin !== location.origin || !linkParts || linkParts[1] !== zim) {
        continue;
      }
      const contentUrl = contentUrlFromRaw(linkUrl, basePath);
      if (!contentUrl) continue;
      const value = articleRouteValue(contentUrl);
      if (value === parentValue) continue;
      if (seen.has(value)) continue;
      seen.add(value);
      links.push(contentUrl);
    }
    const currentValue = articleRouteValue(articleUrl);
    const index = links.findIndex((link) => articleRouteValue(link) === currentValue);
    if (index < 0) return null;
    if (!links[index - 1] && !links[index + 1]) return null;
    const urlAt = (offset) => {
      const link = links[index + offset];
      return link ? articleRouteValue(link) : null;
    };
    return {
      previous: urlAt(-1),
      next: urlAt(1),
    };
  };

  // Returns {navigation, parentUrl}, or null when the caller asked to abort.
  // navigation is null when the article has no parent with sibling links.
  const resolveParentNavigation = async (
    articleUrl,
    {
      basePath = "",
      previousUrl = null,
      fromManualLink = false,
      preferredParentUrl = null,
      fetchImpl = fetch,
      isCurrent = () => true,
    } = {}
  ) => {
    const candidates = preferredParentUrl
      ? [preferredParentUrl]
      : fromManualLink && previousUrl
        ? [previousUrl, ...parentArticleUrls(articleUrl, basePath)]
        : parentArticleUrls(articleUrl, basePath);
    const rawArticle = rawArticleUrl(articleUrl, basePath);
    const seenParents = new Set();
    for (const candidateParentUrl of candidates) {
      if (!candidateParentUrl) continue;
      const parentValue = articleRouteValue(candidateParentUrl);
      if (seenParents.has(parentValue)) continue;
      seenParents.add(parentValue);
      const rawParentUrl = rawArticleUrl(candidateParentUrl, basePath);
      if (!rawParentUrl) continue;
      try {
        const response = await fetchImpl(rawParentUrl);
        if (!response.ok) throw new Error(`HTTP ${response.status}`);
        // Resolve relative links against the final URL: a directory entry may
        // only exist with a trailing slash, which redirects from the plain path.
        const resolvedParentUrl = new URL(
          response.url || rawParentUrl.href,
          location.href
        );
        if (
          rawArticle &&
          articleRouteValue(resolvedParentUrl) === articleRouteValue(rawArticle)
        ) {
          continue;
        }
        const html = await response.text();
        if (!isCurrent()) return null;
        const navigation = readParentArticleNavigation(
          html,
          resolvedParentUrl,
          articleUrl,
          basePath
        );
        if (!navigation) continue;
        const resolvedContentUrl = contentUrlFromRaw(resolvedParentUrl, basePath);
        return {
          navigation,
          parentUrl: (resolvedContentUrl || candidateParentUrl).href,
        };
      } catch (_) {
        if (!isCurrent()) return null;
      }
    }
    return isCurrent() ? {navigation: null, parentUrl: null} : null;
  };

  window.ZimanticParentNavigation = {
    articleRouteValue,
    articleRouteParts,
    rawArticleUrl,
    contentUrlFromRaw,
    parentArticleUrls,
    readParentArticleNavigation,
    resolveParentNavigation,
  };
})();

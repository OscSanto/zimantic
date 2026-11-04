(() => {
    "use strict";

    const catalogs = {
        es: {
            "Primary": "Principal",
            "Search": "Buscar",
            "Library": "Biblioteca",
            "Search your offline library": "Busca en tu biblioteca sin conexión",
            "how to change a lightbulb": "cómo cambiar una bombilla",
            "Search query": "Consulta de búsqueda",
            "Filter by source": "Filtrar por fuente",
            "Search sources": "Buscar fuentes",
            "Search all sources": "Buscar en todas las fuentes",
            "Results pagination": "Paginación de resultados",
            "Your library": "Tu biblioteca",
            "Shuffle": "Mezclar",
            "Filter library": "Filtrar biblioteca",
            "Close ZIM details": "Cerrar los detalles del ZIM",
            "Close article": "Cerrar artículo",
            "Article": "Artículo",
            "Online link": "Enlace en línea",
            "This link points to an online site. It is not cached in your offline library, so opening it may require an internet connection.":
                "Este enlace lleva a un sitio web que no está guardado en tu biblioteca sin conexión. Es posible que necesites conexión a Internet para abrirlo.",
            "Return to article ↩️": "Volver al artículo ↩️",
            "Open online site": "Abrir sitio en línea",
            "Random": "Aleatorio",
            "Open a random article (Alt-click for a random ZIM)":
                "Abrir un artículo aleatorio (Alt+clic para elegir un ZIM aleatorio)",
            "Home": "Inicio",
            "Close": "Cerrar",
            "Permalink": "Enlace permanente",
            "Open or copy this article's permalink": "Abrir o copiar el enlace permanente de este artículo",
            "Search this ZIM": "Buscar en este ZIM",
            "Adjacent search results": "Resultados de búsqueda adyacentes",
            "Previous search result": "Resultado de búsqueda anterior",
            "Open the previous search result": "Abrir el resultado de búsqueda anterior",
            "Next search result": "Siguiente resultado de búsqueda",
            "Open the next search result": "Abrir el siguiente resultado de búsqueda",
            "Adjacent links in parent page": "Enlaces adyacentes de la página principal",
            "Previous link in parent page": "Enlace anterior de la página principal",
            "Open the previous link in the parent page": "Abrir el enlace anterior de la página principal",
            "Next link in parent page": "Siguiente enlace de la página principal",
            "Open the next link in the parent page": "Abrir el siguiente enlace de la página principal",
            "Search your library": "Buscar en tu biblioteca",
            "Link copied": "Enlace copiado",
            "Copy unavailable": "No se pudo copiar el enlace",
            "Details": "Detalles",
            "Open": "Abrir",
            "Random article": "Artículo aleatorio",
            "Unavailable": "No disponible",
            "Loading...": "Cargando...",
            "No results": "Sin resultados",
        },
    };

    const preferredLanguages = [
        ...(Array.isArray(navigator.languages) ? navigator.languages : []),
        navigator.language || "",
    ];
    const locale = preferredLanguages
        .map((language) => language.toLowerCase().split(/[-_]/, 1)[0])
        .find(
            (language) =>
                language === "en" ||
                Object.prototype.hasOwnProperty.call(catalogs, language),
        ) || "en";
    const translations = catalogs[locale] || {};
    // Only explicitly marked UI chrome is translated. Translating every text
    // node also rewrites article titles, search-result titles and metadata
    // (for example an English article called "Library" became "Biblioteca").
    const attributeMarkers = {
        "data-i18n-aria-label": "aria-label",
        "data-i18n-aria-placeholder": "aria-placeholder",
        "data-i18n-aria-valuetext": "aria-valuetext",
        "data-i18n-alt": "alt",
        "data-i18n-placeholder": "placeholder",
        "data-i18n-title": "title",
        "data-i18n-value": "value",
    };
    const markerNames = ["data-i18n", ...Object.keys(attributeMarkers)];
    const markerSelector = markerNames.map((name) => `[${name}]`).join(", ");

    function translate(value) {
        return translations[value] || value;
    }

    function translateElement(element) {
        if (element.nodeType !== Node.ELEMENT_NODE) return;
        if (element.hasAttribute("data-i18n")) {
            const key = element.getAttribute("data-i18n") || element.textContent.trim();
            const translated = translate(key);
            if (key && translated !== element.textContent) {
                element.textContent = translated;
            }
        }
        for (const [marker, target] of Object.entries(attributeMarkers)) {
            if (!element.hasAttribute(marker)) continue;
            const key = element.getAttribute(marker) || element.getAttribute(target) || "";
            const translated = translate(key);
            if (key && translated !== element.getAttribute(target)) {
                element.setAttribute(target, translated);
            }
        }
    }

    function translateTree(root) {
        if (root.nodeType === Node.ELEMENT_NODE) translateElement(root);
        if (!root.querySelectorAll) return;
        for (const element of root.querySelectorAll(markerSelector)) {
            translateElement(element);
        }
    }

    window.zimanticI18n = Object.freeze({ locale, translate, translateTree });
    document.documentElement.lang = locale;
    translateTree(document.documentElement);

    new MutationObserver((mutations) => {
        for (const mutation of mutations) {
            if (mutation.type === "characterData") {
                const parent = mutation.target.parentElement;
                if (parent?.hasAttribute("data-i18n")) translateElement(parent);
            } else if (mutation.type === "attributes") {
                const element = mutation.target;
                if (
                    markerNames.includes(mutation.attributeName) ||
                    element.hasAttribute(`data-i18n-${mutation.attributeName}`)
                ) {
                    translateElement(element);
                }
            } else {
                for (const node of mutation.addedNodes) translateTree(node);
            }
        }
    }).observe(document.documentElement, {
        attributes: true,
        characterData: true,
        childList: true,
        subtree: true,
    });
})();

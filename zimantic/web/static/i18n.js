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
    const attributeNames = [
        "aria-label",
        "aria-placeholder",
        "aria-valuetext",
        "alt",
        "placeholder",
        "title",
        "value",
    ];

    function translate(value) {
        return translations[value] || value;
    }

    function translateNode(node) {
        if (node.nodeType === Node.TEXT_NODE) {
            const value = node.nodeValue || "";
            const trimmed = value.trim();
            if (!trimmed) return;
            const translated = translate(trimmed);
            if (translated !== trimmed) node.nodeValue = value.replace(trimmed, translated);
            return;
        }
        if (node.nodeType !== Node.ELEMENT_NODE) return;

        for (const name of attributeNames) {
            if (!node.hasAttribute(name)) continue;
            const value = node.getAttribute(name);
            const translated = translate(value);
            if (translated !== value) node.setAttribute(name, translated);
        }
        for (const child of node.childNodes) translateNode(child);
    }

    window.zimanticI18n = Object.freeze({ locale, translate });
    document.documentElement.lang = locale;
    translateNode(document.documentElement);

    new MutationObserver((mutations) => {
        for (const mutation of mutations) {
            if (mutation.type === "characterData") {
                translateNode(mutation.target);
            } else if (mutation.type === "attributes") {
                const element = mutation.target;
                const value = element.getAttribute(mutation.attributeName);
                const translated = translate(value);
                if (translated !== value) element.setAttribute(mutation.attributeName, translated);
            } else {
                for (const node of mutation.addedNodes) translateNode(node);
            }
        }
    }).observe(document.documentElement, {
        attributes: true,
        attributeFilter: attributeNames,
        characterData: true,
        childList: true,
        subtree: true,
    });
})();

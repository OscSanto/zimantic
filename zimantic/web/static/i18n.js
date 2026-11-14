(() => {
    "use strict";

    const catalogs = {
        es: {
            "Language": "Idioma",
            "Primary": "Principal",
            "Search": "Buscar",
            "Library": "Biblioteca",
            "Settings": "Configuración",
            "Search your offline library": "Busca en tu biblioteca sin conexión",
            "how to change a lightbulb": "cómo cambiar una bombilla",
            "Search query": "Consulta de búsqueda",
            "Sort by": "Ordenar por",
            "Sort library": "Ordenar biblioteca",
            "Title": "Título",
            "Newest": "Más recientes",
            "Largest": "Más grandes",
            "Most items": "Más elementos",
            "Library pagination": "Paginación de la biblioteca",
            "Relevance": "Relevancia",
            "Filter by source": "Filtrar por fuente",
            "Search sources": "Buscar fuentes",
            "Search all sources": "Buscar en todas las fuentes",
            "Reset source filters": "Restablecer filtros de fuentes",
            "Results pagination": "Paginación de resultados",
            "Your library": "Tu biblioteca",
            "Shuffle": "Mezclar",
            "Filter library": "Filtrar biblioteca",
            "Close ZIM details": "Cerrar los detalles del ZIM",
            "Close source details": "Cerrar los detalles de la fuente",
            "Close article": "Cerrar artículo",
            "Article": "Artículo",
            "Online link": "Enlace en línea",
            "This link points to an online site. It is not cached in your offline library, so opening it may require an internet connection.":
                "Este enlace lleva a un sitio web que no está guardado en tu biblioteca sin conexión. Es posible que necesites conexión a Internet para abrirlo.",
            "Return to article ↩️": "Volver al artículo ↩️",
            "Open online site": "Abrir sitio en línea",
            "Random": "Aleatorio",
            "Browse": "Explorar",
            "Browsing": "Explorando",
            "Browse failed": "La exploración falló",
            "Rare titles": "Títulos poco comunes",
            "Semantic outliers": "Atípicos semánticos",
            "Diversity": "Diversidad",
            "Recency": "Más recientes",
            "Explore a shuffled mix of indexed items.":
                "Explora una mezcla aleatoria de elementos indexados.",
            "Titles with uncommon words in each source.":
                "Títulos con palabras poco comunes en cada fuente.",
            "Items least like their source's average embedding.":
                "Elementos menos parecidos al vector semántico medio de su fuente.",
            "Browsing uses local indexes and respects source filters.":
                "La exploración usa índices locales y respeta los filtros de fuentes.",
            "No indexed items are available for this sort order in the selected sources.":
                "No hay elementos indexados disponibles para este orden en las fuentes seleccionadas.",
            "Open a random article (Alt-click for a random ZIM)":
                "Abrir un artículo aleatorio (Alt+clic para elegir un ZIM aleatorio)",
            "Home": "Inicio",
            "Close": "Cerrar",
            "Permalink": "Enlace permanente",
            "Open or copy this article's permalink": "Abrir o copiar el enlace permanente de este artículo",
            "Search this ZIM": "Buscar en este ZIM",
            "Search this source": "Buscar en esta fuente",
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
            "Copying…": "Copiando…",
            "Copy unavailable": "No se pudo copiar el enlace",
            "Open in a new tab": "Abrir en una pestaña nueva",
            "Dismiss": "Descartar",
            "This article uses browser storage, which is isolated inside this viewer. Changes may not be saved.":
                "Este artículo usa el almacenamiento del navegador, que está aislado dentro de este visor. Es posible que los cambios no se guarden.",
            "This capture has no readable content. The archived page may have been empty (for example, a parked domain), or its scripts and styles may not have been saved.":
                "Esta captura no tiene contenido legible. Puede que la página archivada estuviera vacía (por ejemplo, un dominio aparcado) o que no se guardaran sus scripts y estilos.",
            "Details": "Detalles",
            "Open": "Abrir",
            "Random article": "Artículo aleatorio",
            "Browse files": "Explorar archivos",
            "Unavailable": "No disponible",
            "Loading...": "Cargando...",
            "No results": "Sin resultados",
            "Files": "Archivos",
            "File": "Archivo",
            "Kolibri": "Kolibri",
            "Kiwix": "Kiwix",
            "Remote Kiwix": "Kiwix remoto",
            "Provider": "Proveedor",
            "Filter by provider": "Filtrar por proveedor",
            "All providers": "Todos los proveedores",
            "Filter by language": "Filtrar por idioma",
            "All languages": "Todos los idiomas",
            "Media type": "Tipo de medio",
            "Filter by media type": "Filtrar por tipo de medio",
            "All media": "Todos los medios",
            "Filtered by": "Filtrado por",
            "Reset all filters": "Restablecer todos los filtros",
            "Text": "Texto",
            "Video": "Vídeo",
            "Audio": "Audio",
            "Exercise": "Ejercicio",
            "Interactive": "Interactivo",
            "Document": "Documento",
            "Slideshow": "Presentación",
            "Grade level": "Nivel educativo",
            "Duration": "Duración",
            "Author": "Autor",
            "question": "pregunta",
            "questions": "preguntas",
            "Position in topic": "Posición en el tema",
            "Learning activities": "Actividades de aprendizaje",
            "Create": "Crear",
            "Explore": "Explorar",
            "Listen": "Escuchar",
            "Practice": "Practicar",
            "Read": "Leer",
            "Reflect": "Reflexionar",
            "Watch": "Ver",
            "Image": "Imagen",
            "Images": "Imágenes",
            "Other": "Otros",
            "Search collection": "Buscar en la colección",
            "Source": "Fuente",
            "Indexed": "Indexado",
            "Publisher": "Editor",
            "Creator": "Creador",
            "Date": "Fecha",
            "Size": "Tamaño",
            "Items": "Elementos",
            "item": "elemento",
            "items": "elementos",
            "Category": "Categoría",
            "Collection": "Colección",
            "Media types": "Tipos de medio",
            "Tags": "Etiquetas",
            "License": "Licencia",
            "Origin": "Origen",
            "Attribution": "Atribución",
            "Relation": "Relación",
            "Provider ID": "ID del proveedor",
            "Search failed": "La búsqueda falló",
            "Unknown source": "Fuente desconocida",
            "Choose a source from the list.": "Elige una fuente de la lista.",
            "Unknown collection": "Colección desconocida",
            "Choose a collection from the library.":
                "Elige una colección de la biblioteca.",
            "One or more selected sources are not available.":
                "Una o más fuentes seleccionadas no están disponibles.",
            "Searching for": "Buscando",
            "Searching only in": "Buscando solo en",
            "Searching collection": "Buscando en la colección",
            "source": "fuente",
            "sources": "fuentes",
            "source complete": "fuente completada",
            "sources complete": "fuentes completadas",
            "collection": "colección",
            "collections": "colecciones",
            "loaded source": "fuente cargada",
            "loaded sources": "fuentes cargadas",
            "selected": "seleccionada",
            "Type a query to search it.": "Escribe una consulta para buscarla.",
            "Type a query to search them.": "Escribe una consulta para buscarlas.",
            "and": "y",
            "more": "más",
            "Show": "Mostrar",
            "Previous": "Anterior",
            "Next": "Siguiente",
            "First": "Primera",
            "result": "resultado",
            "results": "resultados",
            "Loading page…": "Cargando página…",
            "No preview available for this result.":
                "No hay vista previa disponible para este resultado.",
            "By": "Por",
            "Show all": "Mostrar todas",
            "Load more sources": "Cargar más fuentes",
            "Showing": "Mostrando",
            "of": "de",
            "Filter": "Filtrar",
            "No matching sources.": "Ninguna fuente coincide.",
            "No sources returned results.": "Ninguna fuente devolvió resultados.",
            "No searchable sources.": "No hay fuentes que se puedan buscar.",
            "No sources are available to search.":
                "No hay fuentes disponibles para buscar.",
            "No results found in the available sources. Try using different words.":
                "No se encontraron resultados en las fuentes disponibles. Prueba con otras palabras.",
            "Discovering sources…": "Descubriendo fuentes…",
            "Loading sources…": "Cargando fuentes…",
            "No sources were discovered in this collection.":
                "No se descubrió ninguna fuente en esta colección.",
            "No sources match these filters.":
                "Ninguna fuente coincide con estos filtros.",
            "No collection or source matches these filters.":
                "Ninguna colección o fuente coincide con estos filtros.",
            "No sources are available in the configured collections.":
                "No hay fuentes disponibles en las colecciones configuradas.",
            "No collections are configured. Add a [[collections]] entry to the server configuration, then restart the server.":
                "No hay colecciones configuradas. Añade una entrada [[collections]] a la configuración del servidor y reinícialo.",
            "Could not load this collection": "No se pudo cargar esta colección",
            "Collection discovery failed": "Falló el descubrimiento de la colección",
            "Could not load the library": "No se pudo cargar la biblioteca",
            "Could not load": "No se pudo cargar",
            "library": "la biblioteca",
            "Using cached source data; refresh failed":
                "Usando datos de fuentes en caché; falló la actualización",
            "All sources failed.": "Todas las fuentes fallaron.",
            "Check your sources and the Kiwix service, then reload or restart the server.":
                "Comprueba tus fuentes y el servicio Kiwix, luego recarga o reinicia el servidor.",
            "This source has no homepage and cannot be searched yet.":
                "Esta fuente no tiene página de inicio y todavía no se puede buscar.",
            "This file cannot be displayed in the offline viewer.":
                "Este archivo no se puede mostrar en el visor sin conexión.",
            "Download file": "Descargar archivo",
        },
    };

    const LOCALE_STORAGE_KEY = "zimantic.locale";
    const normalizeLocale = (value) =>
        String(value || "").toLowerCase().split(/[-_]/, 1)[0];
    const isSupportedLocale = (value) =>
        value === "en" ||
        Object.prototype.hasOwnProperty.call(catalogs, value);
    const readStoredLocale = () => {
        try {
            return localStorage.getItem(LOCALE_STORAGE_KEY) || "";
        } catch (_) {
            return "";
        }
    };

    const preferredLanguages = [
        ...(Array.isArray(navigator.languages) ? navigator.languages : []),
        navigator.language || "",
    ];
    let locale = [
        new URLSearchParams(location.search).get("lang"),
        readStoredLocale(),
        ...preferredLanguages,
    ].map(normalizeLocale).find(isSupportedLocale) || "en";
    let translations = catalogs[locale] || {};
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

    function setLocale(value) {
        const nextLocale = normalizeLocale(value);
        if (!isSupportedLocale(nextLocale)) return false;

        locale = nextLocale;
        translations = catalogs[locale] || {};
        document.documentElement.lang = locale;
        translateTree(document.documentElement);

        try {
            localStorage.setItem(LOCALE_STORAGE_KEY, locale);
        } catch (_) {
            // Locale persistence is optional.
        }

        const picker = document.getElementById("localePicker");
        if (picker) picker.value = locale;
        return true;
    }

    window.zimanticI18n = Object.freeze({
        get locale() {
            return locale;
        },
        translate,
        translateTree,
        setLocale,
    });
    document.documentElement.lang = locale;
    translateTree(document.documentElement);

    const localePicker = document.getElementById("localePicker");
    if (localePicker) {
        localePicker.value = locale;
        localePicker.addEventListener("change", (event) => {
            setLocale(event.target.value);
        });
    }

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

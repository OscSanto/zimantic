(() => {
    "use strict";

    const STORAGE_KEY = "zimantic.theme";
    const THEMES = ["system", "light", "dark"];
    const isTheme = (value) => THEMES.includes(value);
    let current = "system";

    function read() {
        try {
            const value = localStorage.getItem(STORAGE_KEY);
            return isTheme(value) ? value : "system";
        } catch (_) {
            return "system";
        }
    }

    function apply(value) {
        const theme = isTheme(value) ? value : "system";
        const root = document.documentElement;
        if (theme === "system") {
            root.removeAttribute("data-theme");
        } else {
            root.dataset.theme = theme;
        }
        current = theme;
        return theme;
    }

    function set(value) {
        const theme = apply(value);
        try {
            localStorage.setItem(STORAGE_KEY, theme);
        } catch (_) {
            // Persistence is optional in private browsing and sandboxed frames.
        }
        return theme;
    }

    // The article shell runs this file in its own document. Let the host push
    // an explicit theme when the reader changes it while an article is open.
    window.addEventListener("message", (event) => {
        if (event.data?.type === "zimantic-theme") apply(event.data.theme);
    });

    apply(read());

    window.zimanticTheme = Object.freeze({
        get theme() {
            return current;
        },
        read,
        apply,
        set,
    });
})();

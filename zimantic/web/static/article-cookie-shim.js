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

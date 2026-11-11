(() => {
  const makeMemoryStorage = () => {
    const values = new Map();
    const api = {
      getItem: key => values.has(String(key)) ? values.get(String(key)) : null,
      setItem: (key, value) => { values.set(String(key), String(value)); },
      removeItem: key => { values.delete(String(key)); },
      clear: () => { values.clear(); },
      key: index => [...values.keys()][index] ?? null,
    };
    return new Proxy(api, {
      get(target, property) {
        if (property === "length") return values.size;
        if (property in target) return target[property];
        if (typeof property === "string" && values.has(property)) {
          return values.get(property);
        }
        return undefined;
      },
      set(target, property, value) {
        if (typeof property !== "string" || property in target) return false;
        values.set(property, String(value));
        return true;
      },
      deleteProperty(target, property) {
        values.delete(String(property));
        return true;
      },
      has(target, property) {
        return property in target || values.has(String(property));
      },
      ownKeys() {
        return [...values.keys()];
      },
      getOwnPropertyDescriptor(target, property) {
        if (!values.has(String(property))) return undefined;
        return {
          configurable: true,
          enumerable: true,
          value: values.get(String(property)),
          writable: true,
        };
      },
    });
  };
  const report = name => {
    try {
      if (window.parent !== window) {
        window.parent.postMessage({
          type: "zimantic-article-storage-blocked",
          storage: name,
        }, "*");
      }
    } catch (_) {}
  };
  for (const name of ["localStorage", "sessionStorage"]) {
    let descriptor = Object.getOwnPropertyDescriptor(window, name);
    let prototype = Object.getPrototypeOf(window);
    while (!descriptor && prototype) {
      descriptor = Object.getOwnPropertyDescriptor(prototype, name);
      prototype = Object.getPrototypeOf(prototype);
    }
    let blocked = false;
    try {
      if (descriptor && typeof descriptor.get === "function") {
        descriptor.get.call(window);
      } else {
        void window[name];
      }
    } catch (_) {
      blocked = true;
    }
    if (!blocked) continue;
    let storage = null;
    let reported = false;
    try {
      Object.defineProperty(window, name, {
        configurable: true,
        enumerable: true,
        get: () => {
          if (!storage) storage = makeMemoryStorage();
          if (!reported) {
            reported = true;
            report(name);
          }
          return storage;
        },
        set: () => {},
      });
    } catch (_) {}
  }
})();

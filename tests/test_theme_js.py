import json
import shutil
import subprocess
import unittest
from pathlib import Path

THEME_JS = Path(__file__).resolve().parents[1] / "zimantic" / "web" / "static" / "theme.js"
NODE = shutil.which("node")


@unittest.skipUnless(NODE, "node is unavailable")
class ThemeJsTests(unittest.TestCase):
    def _run(self, stored=None):
        script = f"""
const store = {{ "zimantic.theme": {json.dumps(stored)} }};
const root = {{
  dataset: {{}},
  removed: null,
  removeAttribute(name) {{ this.removed = name; delete this.dataset.theme; }},
}};
const listeners = {{}};
global.document = {{documentElement: root}};
global.localStorage = {{
  getItem(key) {{ return Object.prototype.hasOwnProperty.call(store, key) ? store[key] : null; }},
  setItem(key, value) {{ store[key] = String(value); }},
}};
global.window = {{
  addEventListener(type, handler) {{ listeners[type] = handler; }},
}};
{THEME_JS.read_text(encoding="utf-8")}
const snapshot = () => ({{
  theme: window.zimanticTheme.theme,
  attr: root.dataset.theme || null,
  removed: root.removed,
  stored: Object.prototype.hasOwnProperty.call(store, "zimantic.theme")
    ? store["zimantic.theme"] : null,
}});
const results = {{initial: snapshot()}};
results.setDark = window.zimanticTheme.set("dark");
results.afterSetDark = snapshot();
results.setInvalid = window.zimanticTheme.set("sepia");
results.afterInvalid = snapshot();
listeners.message({{data: {{type: "zimantic-theme", theme: "light"}}}});
results.afterMessage = snapshot();
results.ignoredMessage = (() => {{
  listeners.message({{data: {{type: "other", theme: "dark"}}}});
  return snapshot();
}})();
console.log(JSON.stringify(results));
"""
        result = subprocess.run(
            [NODE, "-e", script], check=True, encoding="utf-8", capture_output=True
        )
        return json.loads(result.stdout)

    def test_missing_setting_follows_system_scheme(self):
        payload = self._run()
        self.assertEqual(
            payload["initial"],
            {
                "theme": "system",
                "attr": None,
                "removed": "data-theme",
                "stored": None,
            },
        )

    def test_saved_setting_is_applied_before_the_page_paints(self):
        payload = self._run("light")
        self.assertEqual(payload["initial"]["theme"], "light")
        self.assertEqual(payload["initial"]["attr"], "light")
        self.assertIsNone(payload["initial"]["removed"])

    def test_invalid_saved_setting_falls_back_to_system(self):
        payload = self._run("sepia")
        self.assertEqual(payload["initial"]["theme"], "system")
        self.assertIsNone(payload["initial"]["attr"])

    def test_set_persists_explicit_theme_and_resets_to_system(self):
        payload = self._run()
        self.assertEqual(payload["setDark"], "dark")
        self.assertEqual(payload["afterSetDark"]["attr"], "dark")
        self.assertEqual(payload["afterSetDark"]["stored"], "dark")
        self.assertEqual(payload["setInvalid"], "system")
        self.assertEqual(payload["afterInvalid"]["theme"], "system")
        self.assertIsNone(payload["afterInvalid"]["attr"])
        self.assertEqual(payload["afterInvalid"]["stored"], "system")

    def test_host_message_updates_theme_without_changing_the_saved_choice(self):
        payload = self._run("dark")
        self.assertEqual(payload["afterMessage"]["theme"], "light")
        self.assertEqual(payload["afterMessage"]["attr"], "light")
        self.assertEqual(payload["afterMessage"]["stored"], "system")
        self.assertEqual(payload["ignoredMessage"]["theme"], "light")


if __name__ == "__main__":
    unittest.main()

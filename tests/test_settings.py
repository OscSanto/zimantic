import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from zimantic import settings
from zimantic.settings import PROFILES, load_config, profile_for_hardware


class HardwareProfileTests(unittest.TestCase):
    @patch.object(settings, "_system_memory_gb", return_value=0.5)
    def test_low_memory_is_mobile(self, _memory):
        with patch("os.cpu_count", return_value=4):
            self.assertEqual(profile_for_hardware(), "mobile")

    @patch.object(settings, "_system_memory_gb", return_value=8.0)
    def test_mid_memory_is_desktop(self, _memory):
        with patch("os.cpu_count", return_value=8):
            self.assertEqual(profile_for_hardware(), "desktop")

    @patch.object(settings, "_system_memory_gb", return_value=256.0)
    def test_lots_of_memory_is_supercomputer(self, _memory):
        with patch("os.cpu_count", return_value=16):
            self.assertEqual(profile_for_hardware(), "supercomputer")

    @patch.object(settings, "_system_memory_gb", return_value=16.0)
    def test_many_cores_is_supercomputer(self, _memory):
        with patch("os.cpu_count", return_value=64):
            self.assertEqual(profile_for_hardware(), "supercomputer")


class LoadConfigTests(unittest.TestCase):
    def test_missing_file_warns_and_uses_a_complete_profile(self):
        warnings = []
        load_config("/nonexistent/config.toml", warn=warnings.append)

        self.assertTrue(warnings)
        self.assertIn("not found", warnings[0])
        # Every profile carries the keys build/serve read without fallback.
        for profile in PROFILES.values():
            for key in ("index_dir", "zim_dir", "model_dir", "port", "batch_size"):
                self.assertIn(key, profile)

    def test_empty_file_warns(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.toml"
            path.write_text("", encoding="utf-8")
            warnings = []
            load_config(path, warn=warnings.append)
            self.assertTrue(any("empty" in message for message in warnings))

    def test_malformed_file_warns_instead_of_failing(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.toml"
            path.write_text("this is not = = valid toml", encoding="utf-8")
            warnings = []
            cfg = load_config(path, warn=warnings.append)
            self.assertTrue(warnings)
            self.assertEqual(cfg["index_dir"], "./indexes")

    def test_user_keys_override_and_missing_keys_stay_defaulted(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.toml"
            path.write_text('port = 9999\ncache_size = 7\n', encoding="utf-8")
            warnings = []
            cfg = load_config(path, warn=warnings.append)
            self.assertEqual(cfg["port"], 9999)
            self.assertEqual(cfg["cache_size"], 7)
            self.assertEqual(cfg["index_dir"], "./indexes")  # filled from the profile
            self.assertEqual(warnings, [])


if __name__ == "__main__":
    unittest.main()
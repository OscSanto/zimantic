import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from zimantic import settings
from zimantic.settings import (
    PROFILES,
    default_build_workers,
    default_embedding_workers,
    load_config,
    normalize_web_base_path,
    profile_for_hardware,
)


class HardwareProfileTests(unittest.TestCase):
    def test_web_base_path_is_normalized(self):
        self.assertEqual(normalize_web_base_path("zimantic/"), "/zimantic")
        self.assertEqual(normalize_web_base_path("/zimantic"), "/zimantic")
        self.assertEqual(normalize_web_base_path(" /zimantic/ "), "/zimantic")
        self.assertEqual(normalize_web_base_path("/"), "")
        self.assertEqual(normalize_web_base_path("///"), "")
        self.assertEqual(normalize_web_base_path(""), "")
        self.assertEqual(normalize_web_base_path(None), "")

    @patch.object(settings, "_system_memory_gb", return_value=0.5)
    def test_low_memory_is_mobile(self, _memory):
        with patch("os.cpu_count", return_value=4):
            self.assertEqual(profile_for_hardware(), "mobile")

    @patch.object(settings, "_system_memory_gb", return_value=2.0)
    def test_two_gb_hosts_use_mobile_profile(self, _memory):
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

    def test_build_workers_scale_with_cpu_and_memory(self):
        self.assertEqual(default_build_workers(4, 0.5), 1)
        self.assertEqual(default_build_workers(8, 8), 2)
        self.assertEqual(default_build_workers(20, 32), 5)
        self.assertEqual(default_build_workers(64, 256), 8)

    def test_embedding_workers_respect_thread_budget(self):
        self.assertEqual(default_embedding_workers(4, 0.5, 1, 2), 1)
        self.assertEqual(default_embedding_workers(20, 32, 5, None), 1)
        self.assertEqual(default_embedding_workers(32, 128, 8, 8), 4)


class LoadConfigTests(unittest.TestCase):
    def test_missing_file_warns_and_uses_a_complete_profile(self):
        warnings = []
        with patch.object(settings, "profile_for_hardware", return_value="desktop"):
            cfg = load_config("/nonexistent/config.toml", warn=warnings.append)

        self.assertTrue(warnings)
        self.assertIn("not found", warnings[0])
        # The returned config, not just the profile constants, must be complete.
        for key in ("index_dir", "zim_dir", "model_dir", "port", "batch_size"):
            self.assertIn(key, cfg)
            self.assertEqual(cfg[key], PROFILES["desktop"][key])

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
            # A parse failure is not an empty file; the second warning used to
            # contradict the "could not read" one.
            self.assertFalse(any("empty" in message for message in warnings))

    def test_unreadable_path_is_not_reported_as_empty(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp) / "config.toml"
            directory.mkdir()
            warnings = []
            load_config(directory, warn=warnings.append)
            self.assertTrue(any("could not read" in message for message in warnings))
            self.assertFalse(any("empty" in message for message in warnings))

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

    def test_worker_defaults_are_dynamic_but_explicit_values_override(self):
        with patch.object(settings, "profile_for_hardware", return_value="desktop"):
            with patch.object(settings, "default_build_workers", return_value=5):
                with patch.object(settings, "default_embedding_workers", return_value=3):
                    cfg = load_config("/nonexistent/config.toml", warn=lambda _: None)
        self.assertEqual(cfg["build_workers"], 5)
        self.assertEqual(cfg["embedding_workers"], 3)

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.toml"
            path.write_text("build_workers = 7\nembedding_workers = 6\n", encoding="utf-8")
            with patch.object(settings, "profile_for_hardware", return_value="desktop"):
                cfg = load_config(path, warn=lambda _: None)
            self.assertEqual(cfg["build_workers"], 7)
            self.assertEqual(cfg["embedding_workers"], 6)

    def test_embedding_default_uses_configured_embed_threads(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.toml"
            path.write_text("embed_threads = 3\n", encoding="utf-8")
            with (
                patch.object(settings, "profile_for_hardware", return_value="desktop"),
                patch.object(settings, "default_build_workers", return_value=5),
                patch.object(settings, "default_embedding_workers", return_value=2) as workers,
            ):
                load_config(path, warn=lambda _: None)
        workers.assert_called_once_with(build_workers=5, embed_threads=3)

    def test_unknown_keys_warn_but_known_keys_still_apply(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.toml"
            path.write_text('port = 9999\nbath_size = 8\n', encoding="utf-8")
            warnings = []
            cfg = load_config(path, warn=warnings.append)
            self.assertEqual(cfg["port"], 9999)
            self.assertTrue(any("bath_size" in message for message in warnings))
            self.assertNotIn("bath_size", cfg)

    def test_bad_value_types_warn_and_keep_profile_defaults(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.toml"
            path.write_text(
                'port = "not a port"\n'
                'batch_size = "8"\n'
                'min_cosine_similarity = "high"\n'
                'cache_size = 7\n'
                'build_workers = "two"\n',
                encoding="utf-8",
            )
            warnings = []
            with patch.object(settings, "profile_for_hardware", return_value="desktop"):
                cfg = load_config(path, warn=warnings.append)
            self.assertEqual(cfg["port"], PROFILES["desktop"]["port"])
            self.assertEqual(cfg["batch_size"], PROFILES["desktop"]["batch_size"])
            self.assertEqual(
                cfg["min_cosine_similarity"],
                PROFILES["desktop"]["min_cosine_similarity"],
            )
            self.assertEqual(cfg["cache_size"], 7)
            self.assertGreaterEqual(cfg["build_workers"], 1)
            self.assertTrue(any("port" in message for message in warnings))
            self.assertTrue(any("batch_size" in message for message in warnings))
            self.assertTrue(any("min_cosine_similarity" in message for message in warnings))

    def test_shipped_config_leaves_tuning_to_the_hardware_profile(self):
        root_config = Path(__file__).resolve().parents[1] / "config.toml"
        with patch.object(settings, "profile_for_hardware", return_value="mobile"):
            cfg = load_config(root_config, warn=lambda _message: None)
        for key in ("batch_size", "max_entry_bytes", "search_workers", "cache_size"):
            self.assertEqual(cfg[key], PROFILES["mobile"][key])


if __name__ == "__main__":
    unittest.main()
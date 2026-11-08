import os
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

    @patch.object(settings, "_system_memory_gb", return_value=1.0)
    def test_one_gb_hosts_use_mobile_profile(self, _memory):
        with patch("os.cpu_count", return_value=4):
            self.assertEqual(profile_for_hardware(), "mobile")

    @patch.object(settings, "_system_memory_gb", return_value=2.0)
    def test_two_gb_hosts_use_desktop_profile(self, _memory):
        with patch("os.cpu_count", return_value=4):
            self.assertEqual(profile_for_hardware(), "desktop")

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
        self.assertEqual(default_embedding_workers(20, 32, 5, None), 8)
        self.assertEqual(default_embedding_workers(32, 128, 8, 8), 3)
        self.assertEqual(default_embedding_workers(4, 8, 1, None), 3)
        self.assertEqual(default_embedding_workers(20, 0.5, 5, None), 1)
        self.assertEqual(default_embedding_workers(20, 1, 5, None, 8000), 1)


class LoadConfigTests(unittest.TestCase):
    @unittest.skipIf(os.name == "nt", "XDG config paths are Unix-only")
    def test_xdg_defaults_and_relative_overrides_use_data_home(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            work = root / "work"
            work.mkdir()
            config_home = root / "config"
            config_file = config_home / "zimantic" / "config.toml"
            config_file.parent.mkdir(parents=True)
            config_file.write_text(
                'model_dir = "models/fp16"\n'
                'index_dir = "indexes"\n'
                '[[collections]]\n'
                'type = "zim"\n'
                'name = "library"\n'
                'path = "archives"\n',
                encoding="utf-8",
            )
            data_home = root / "data"
            with (
                patch.dict(
                    os.environ,
                    {
                        "XDG_CONFIG_HOME": str(config_home),
                        "XDG_DATA_HOME": str(data_home),
                    },
                ),
                patch("zimantic.paths.Path.cwd", return_value=work),
                patch.object(settings, "profile_for_hardware", return_value="desktop"),
            ):
                cfg = load_config(warn=lambda _message: None)

        data_dir = data_home / "zimantic"
        self.assertEqual(
            cfg["collections"][0].path, str(config_file.parent / "archives")
        )
        self.assertEqual(cfg["model_dir"], str(data_dir / "models/fp16"))
        self.assertEqual(cfg["index_dir"], str(data_dir / "indexes"))

    @unittest.skipIf(os.name == "nt", "XDG config paths are Unix-only")
    def test_xdg_defaults_apply_without_a_config_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            work = root / "work"
            work.mkdir()
            config_home = root / "config"
            data_home = root / "data"
            with (
                patch.dict(
                    os.environ,
                    {
                        "XDG_CONFIG_HOME": str(config_home),
                        "XDG_DATA_HOME": str(data_home),
                    },
                ),
                patch("zimantic.paths.Path.cwd", return_value=work),
                patch.object(settings, "profile_for_hardware", return_value="desktop"),
            ):
                cfg = load_config(warn=lambda _message: None)

        data_dir = data_home / "zimantic"
        self.assertEqual(cfg["zim_dir"], str(data_dir / "zims"))
        self.assertEqual(cfg["model_dir"], str(data_dir / "model/int8"))
        self.assertEqual(cfg["index_dir"], str(data_dir / "indexes"))

    @unittest.skipIf(os.name == "nt", "XDG config paths are Unix-only")
    def test_cwd_config_is_ignored_and_xdg_data_defaults_are_used(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            work = root / "work"
            work.mkdir()
            (work / "config.toml").write_text("port = 8999\n", encoding="utf-8")
            config_home = root / "config"
            data_home = root / "data"
            with (
                patch.dict(
                    os.environ,
                    {
                        "XDG_CONFIG_HOME": str(config_home),
                        "XDG_DATA_HOME": str(data_home),
                    },
                ),
                patch("zimantic.paths.Path.cwd", return_value=work),
                patch.object(settings, "profile_for_hardware", return_value="desktop"),
            ):
                cfg = load_config(warn=lambda _message: None)

        data_dir = data_home / "zimantic"
        self.assertEqual(cfg["port"], PROFILES["desktop"]["port"])
        self.assertEqual(cfg["zim_dir"], str(data_dir / "zims"))
        self.assertEqual(cfg["model_dir"], str(data_dir / "model/int8"))
        self.assertEqual(cfg["index_dir"], str(data_dir / "indexes"))

    def test_explicit_config_path_keeps_existing_relative_defaults(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.toml"
            path.write_text("port = 8999\n", encoding="utf-8")
            with (
                patch.object(settings, "xdg_data_dir", side_effect=AssertionError),
                patch.object(settings, "profile_for_hardware", return_value="desktop"),
            ):
                cfg = load_config(path, warn=lambda _message: None)

        self.assertEqual(cfg["port"], 8999)
        self.assertEqual(cfg["index_dir"], "./indexes")

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

    def test_slot_wait_seconds_is_a_known_float_setting(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.toml"
            path.write_text("slot_wait_seconds = 3.5\n", encoding="utf-8")
            warnings = []
            with patch.object(settings, "profile_for_hardware", return_value="desktop"):
                cfg = load_config(path, warn=warnings.append)
            self.assertEqual(cfg["slot_wait_seconds"], 3.5)
            self.assertFalse(any("unknown config key" in message for message in warnings))

    def test_empty_file_warns(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.toml"
            path.write_text("", encoding="utf-8")
            warnings = []
            load_config(path, warn=warnings.append)
            self.assertTrue(any("empty" in message for message in warnings))

    def test_malformed_file_is_fatal(self):
        from zimantic.providers.config import ConfigValidationError

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.toml"
            path.write_text("this is not = = valid toml", encoding="utf-8")
            with self.assertRaises(ConfigValidationError):
                load_config(path, warn=lambda _message: None)

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

    def test_secure_browsing_defaults_off_and_accepts_boolean(self):
        self.assertFalse(PROFILES["desktop"]["secure_browsing"])
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.toml"
            path.write_text("secure_browsing = true\n", encoding="utf-8")
            warnings = []
            cfg = load_config(path, warn=warnings.append)
            self.assertTrue(cfg["secure_browsing"])
            self.assertEqual(warnings, [])

    def test_secure_browsing_rejects_non_boolean(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.toml"
            path.write_text('secure_browsing = "yes"\n', encoding="utf-8")
            warnings = []
            cfg = load_config(path, warn=warnings.append)
            self.assertFalse(cfg["secure_browsing"])
            self.assertTrue(any("secure_browsing" in message for message in warnings))

    def test_zero_worker_counts_warn_and_fall_back_to_automatic_defaults(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.toml"
            path.write_text("build_workers = 0\nembedding_workers = 0\n", encoding="utf-8")
            warnings = []
            with (
                patch.object(settings, "profile_for_hardware", return_value="desktop"),
                patch.object(settings, "default_build_workers", return_value=5),
                patch.object(settings, "default_embedding_workers", return_value=3),
            ):
                cfg = load_config(path, warn=warnings.append)
        self.assertEqual(cfg["build_workers"], 5)
        self.assertEqual(cfg["embedding_workers"], 3)
        self.assertTrue(any("build_workers" in message for message in warnings))
        self.assertTrue(any("embedding_workers" in message for message in warnings))

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

    def test_embedding_default_uses_build_not_serving_threads(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.toml"
            for profile, build_threads, expected in (
                ("desktop", None, 1),
                ("desktop", 3, 3),
                ("mobile", None, 2),
                ("supercomputer", None, 8),
            ):
                with self.subTest(profile=profile, build_threads=build_threads):
                    text = "embed_threads = 8\n"
                    if build_threads is not None:
                        text += f"build_embed_threads = {build_threads}\n"
                    path.write_text(text, encoding="utf-8")
                    with (
                        patch.object(settings, "profile_for_hardware", return_value=profile),
                        patch.object(settings, "default_build_workers", return_value=5),
                        patch.object(settings, "default_embedding_workers", return_value=2) as workers,
                    ):
                        cfg = load_config(path, warn=lambda _: None)
                    self.assertEqual(cfg["embed_threads"], 8)
                    self.assertEqual(cfg["build_embed_threads"], expected)
                    workers.assert_called_once_with(
                        build_workers=5,
                        build_embed_threads=expected,
                        token_budget=1000,
                    )

    def test_invalid_build_threads_fall_back_to_one(self):
        with (
            tempfile.TemporaryDirectory() as tmp,
            patch.object(settings, "profile_for_hardware", return_value="desktop"),
        ):
            path = Path(tmp) / "config.toml"
            for value in ("0", "-1", "true", '"two"'):
                with self.subTest(value=value):
                    path.write_text(f"build_embed_threads = {value}\n", encoding="utf-8")
                    warnings = []
                    cfg = load_config(path, warn=warnings.append)
                    self.assertEqual(cfg["build_embed_threads"], 1)
                    self.assertEqual(
                        len([msg for msg in warnings if "build_embed_threads" in msg]),
                        1,
                    )

    def test_embed_device_defaults_to_auto_and_accepts_a_string(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.toml"
            path.write_text('embed_device = "cuda"\n', encoding="utf-8")
            warnings = []
            cfg = load_config(path, warn=warnings.append)
            self.assertEqual(cfg["embed_device"], "cuda")
            self.assertEqual(warnings, [])

        with patch.object(settings, "profile_for_hardware", return_value="desktop"):
            cfg = load_config("/nonexistent/config.toml", warn=lambda _: None)
        self.assertEqual(cfg["embed_device"], "auto")

    def test_embed_device_is_normalized(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.toml"
            path.write_text('embed_device = "  CUDA  "\n', encoding="utf-8")
            warnings = []
            cfg = load_config(path, warn=warnings.append)
        self.assertEqual(cfg["embed_device"], "cuda")
        self.assertEqual(warnings, [])

    def test_embed_device_rejects_an_unknown_name(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.toml"
            path.write_text('embed_device = "tensor-core"\n', encoding="utf-8")
            warnings = []
            cfg = load_config(path, warn=warnings.append)
        self.assertEqual(cfg["embed_device"], "auto")
        self.assertTrue(
            any("embed_device" in message and "tensor-core" in message
                for message in warnings)
        )

    def test_embed_device_rejects_non_string(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.toml"
            path.write_text("embed_device = 1\n", encoding="utf-8")
            warnings = []
            cfg = load_config(path, warn=warnings.append)
        self.assertEqual(cfg["embed_device"], "auto")
        self.assertTrue(any("embed_device" in message for message in warnings))

    def test_collection_derived_text_defaults_and_source_overrides(self):
        from types import SimpleNamespace

        from zimantic.indexing.derived import source_operations

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.toml"
            path.write_text(
                '[[collections]]\n'
                'type = "files"\n'
                'name = "media"\n'
                'path = "media"\n'
                'derived_text = { captions = "index", ocr = "tesseract", asr = "whisper" }\n'
                'sources = [\n'
                '  { key = "files:media:captions-off", captions = "off" },\n'
                '  { key = "files:media:ocr-off", ocr = "off" },\n'
                ']\n',
                encoding="utf-8",
            )
            cfg = load_config(path, warn=lambda _: None)

        def source(key):
            return SimpleNamespace(key=key, collection_key="files:media")

        inherited = source_operations(cfg, source("files:media:default"))
        self.assertEqual(inherited.state(), {
            "captions": "index",
            "ocr": "tesseract",
            "asr": "whisper",
        })
        captions_off = source_operations(
            cfg, source("files:media:captions-off")
        )
        self.assertEqual(captions_off.state(), {
            "captions": "off",
            "ocr": "tesseract",
            "asr": "whisper",
        })
        ocr_off = source_operations(cfg, source("files:media:ocr-off"))
        self.assertEqual(ocr_off.state(), {
            "captions": "index",
            "ocr": "off",
            "asr": "whisper",
        })

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
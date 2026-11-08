import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from zimantic.paths import (
    default_config_path,
    reload_pid_path,
    runtime_pid_path,
    server_pid_path,
    xdg_config_home,
    xdg_data_home,
)


@unittest.skipIf(os.name == "nt", "XDG config and runtime paths are Unix-only")
class XdgPathTests(unittest.TestCase):
    def test_xdg_config_path_is_used_even_when_cwd_config_exists(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            xdg_config = root / "config"
            xdg_file = xdg_config / "zimantic" / "config.toml"
            xdg_file.parent.mkdir(parents=True)
            xdg_file.touch()
            cwd = root / "work"
            cwd.mkdir()
            (cwd / "config.toml").touch()

            with patch.dict(os.environ, {"XDG_CONFIG_HOME": str(xdg_config)}):
                selected = default_config_path(cwd)

        self.assertEqual(selected, xdg_file)

    def test_cwd_config_is_not_used_when_xdg_config_is_missing(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cwd = root / "work"
            cwd.mkdir()
            legacy_file = cwd / "config.toml"
            legacy_file.touch()
            with patch.dict(os.environ, {"XDG_CONFIG_HOME": str(root / "config")}):
                selected = default_config_path(cwd)

        self.assertEqual(selected, root / "config" / "zimantic" / "config.toml")
        self.assertNotEqual(selected, legacy_file)

    def test_missing_config_uses_xdg_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cwd = root / "work"
            cwd.mkdir()
            config_home = root / "config"
            with patch.dict(os.environ, {"XDG_CONFIG_HOME": str(config_home)}):
                selected = default_config_path(cwd)

        self.assertEqual(selected, config_home / "zimantic" / "config.toml")

    def test_relative_xdg_homes_use_spec_defaults(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            with (
                patch.dict(
                    os.environ,
                    {
                        "HOME": str(home),
                        "XDG_CONFIG_HOME": "relative-config",
                        "XDG_DATA_HOME": "relative-data",
                    },
                ),
                patch("zimantic.paths.Path.home", return_value=home),
            ):
                self.assertEqual(xdg_config_home(), home / ".config")
                self.assertEqual(xdg_data_home(), home / ".local/share")

    def test_runtime_pid_uses_xdg_runtime_dir_when_available(self):
        with tempfile.TemporaryDirectory() as tmp:
            runtime = Path(tmp) / "runtime"
            runtime.mkdir()
            with patch.dict(os.environ, {"XDG_RUNTIME_DIR": str(runtime)}):
                expected = runtime / "zimantic" / "zimantic.pid"
                self.assertEqual(runtime_pid_path(), expected)
                self.assertEqual(server_pid_path(), expected)
                self.assertEqual(reload_pid_path(), expected)

    def test_runtime_pid_falls_back_when_runtime_dir_is_unavailable(self):
        with patch.dict(os.environ, {"XDG_RUNTIME_DIR": "relative-runtime"}):
            self.assertIsNone(runtime_pid_path())
            self.assertEqual(server_pid_path(), Path("zimantic.pid"))


if __name__ == "__main__":
    unittest.main()

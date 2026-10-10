import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


class StableSizeTests(unittest.TestCase):
    def test_waits_for_an_unchanged_size_regardless_of_file_size(self):
        import zimantic.__main__ as main_module

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "large.zim"
            path.write_bytes(b"x" * (5 * 1024 * 1024 + 1))

            with patch.object(main_module.time, "sleep") as sleep:
                main_module._wait_for_stable_size(path)

        sleep.assert_called_once_with(main_module.STABLE_POLL_SECONDS)


if __name__ == "__main__":
    unittest.main()

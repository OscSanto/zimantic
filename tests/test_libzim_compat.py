import unittest

from zimantic.libzim_compat import get_entry_by_id, search_paths


class _OldArchive:
    def _get_entry_by_id(self, entry_id):
        return f"old:{entry_id}"


class _NewArchive:
    def get_entry_by_id(self, entry_id):
        return f"new:{entry_id}"


class _OldSearch:
    def getResults(self, start, count):
        return ["alpha", "beta"][start:start + count]


class _NewSearch:
    def get_results(self, start, count):
        return [
            type("Result", (), {"path": path})()
            for path in ["alpha", "beta"][start:start + count]
        ]


class LibzimCompatTests(unittest.TestCase):
    def test_entry_lookup_works_with_both_api_names(self):
        self.assertEqual(get_entry_by_id(_OldArchive(), 3), "old:3")
        self.assertEqual(get_entry_by_id(_NewArchive(), 3), "new:3")

    def test_search_paths_works_with_both_result_shapes(self):
        self.assertEqual(search_paths(_OldSearch(), 0, 2), ["alpha", "beta"])
        self.assertEqual(search_paths(_NewSearch(), 0, 2), ["alpha", "beta"])
        self.assertEqual(search_paths(_NewSearch(), 1, 1), ["beta"])


if __name__ == "__main__":
    unittest.main()

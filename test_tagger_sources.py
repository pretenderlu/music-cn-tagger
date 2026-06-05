import unittest

import tagger


class SearchSourceTests(unittest.TestCase):
    def test_default_sources_use_itunes_only(self):
        self.assertEqual(tagger.ScanOptions().sources, ("itunes",))
        self.assertEqual(tagger.normalize_sources(None), ("itunes",))

    def test_sources_are_cleaned_deduped_and_ordered(self):
        self.assertEqual(
            tagger.normalize_sources(" itunes, netease, itunes "),
            ("itunes", "netease"),
        )
        self.assertEqual(
            tagger.normalize_sources(["NetEase", "itunes"]),
            ("netease", "itunes"),
        )

    def test_empty_or_unknown_sources_are_rejected(self):
        with self.assertRaises(ValueError):
            tagger.normalize_sources([])
        with self.assertRaises(ValueError):
            tagger.normalize_sources(["spotify"])


if __name__ == "__main__":
    unittest.main()

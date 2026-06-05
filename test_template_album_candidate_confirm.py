import unittest
from pathlib import Path


class AlbumCandidateTemplateTests(unittest.TestCase):
    def test_auto_album_candidates_can_be_confirmed_with_their_folder_and_source(self):
        html = Path("templates/index.html").read_text(encoding="utf-8")

        self.assertIn('@click="confirmCandidate(c)"', html)
        self.assertIn("folder: candidate.folder || this.srcDir", html)
        self.assertIn("source: candidate.source || 'itunes'", html)


if __name__ == "__main__":
    unittest.main()

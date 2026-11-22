"""T-0178: no-notes card states source, tag and build date."""
import sys, unittest
from datetime import datetime, timezone
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import build

NOW = datetime(2026, 11, 22, 6, 0, tzinfo=timezone.utc)


def card(kind, repo, flagged=False):
    return build.render_card({
        "id": "pypi/x/1.0", "ecosystem": "pypi", "name": "x", "version": "1.0", "date": "2026-11-21", "prerelease": False,
        "yanked": False, "notes_url": "https://pypi.org/project/x/", "notes_kind": kind, "notes_go": "abc", "bump": "minor",
        "prev_version": "0.9", "flagged": flagged, "quotes": [], "no_notes": False, "no_notes_checked": None,
        "pkg_path": "/pkg/pypi/x/", "errata_url": None, "errata_go": None, "gh_repo": repo}, NOW)


class NoNotes(unittest.TestCase):
    def test_repo_known(self):
        h = card("repo_releases", "o/r")
        self.assertIn("No release notes found on GitHub Releases for o/r at tag 1.0 · checked 2026-11-22 UTC", h)

    def test_no_repo(self):
        self.assertIn("No GitHub repository known for this package · checked 2026-11-22 UTC", card("registry_page", None))

    def test_with_notes(self):
        h = card("github_release", "o/r")
        self.assertNotIn("No release notes found", h)
        self.assertNotIn("No GitHub repository known", h)


if __name__ == "__main__":
    unittest.main()

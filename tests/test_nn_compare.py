"""T-0180: no-notes card links the GitHub compare view via /go/."""
import sys, unittest
from datetime import datetime, timezone
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import build

NOW = datetime(2026, 11, 22, 6, 0, tzinfo=timezone.utc)
GH = [{"tag": "v1.1"}, {"tag": "v1.0"}]


def rel(prev="1.0", kind="repo_releases"):
    return {"id": "pypi/x/1.1", "ecosystem": "pypi", "name": "x", "version": "1.1", "date": "2026-11-21", "prerelease": False,
            "yanked": False, "notes_url": "https://github.com/o/r/releases", "notes_kind": kind, "notes_go": "abc", "bump": "minor",
            "prev_version": prev, "flagged": False, "quotes": [], "no_notes": True, "no_notes_checked": "2026-11-22",
            "pkg_path": "/pkg/pypi/x/", "errata_url": None, "errata_go": None, "gh_repo": "o/r"}


class C(unittest.TestCase):
    def test_repo_prev(self):
        r = rel(); build.nn_compare(r, "o/r", GH, "x")
        self.assertEqual(r["compare_url"], "https://github.com/o/r/compare/v1.0...v1.1")
        self.assertIn("Compare 1.0…1.1 on GitHub", build.render_card(r, NOW))
        self.assertIn(f'/go/compare/{r["compare_go"]}/', build.render_card(r, NOW))

    def test_no_repo(self):
        r = rel(); build.nn_compare(r, None, GH, "x")
        self.assertNotIn("compare_url", r)
        self.assertNotIn("Compare", build.render_card(r, NOW))

    def test_no_prev(self):
        r = rel(prev=None); build.nn_compare(r, "o/r", GH, "x")
        self.assertNotIn("compare_url", r)

    def test_with_notes(self):
        r = rel(kind="github_release"); build.nn_compare(r, "o/r", GH, "x")
        self.assertNotIn("compare_url", r)


if __name__ == "__main__":
    unittest.main()

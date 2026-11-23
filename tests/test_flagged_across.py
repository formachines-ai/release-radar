"""T-0182: Flagged across versions section and #v<version> anchors on /pkg/ pages."""
import sys, unittest
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import build


def rec(v, date, flagged, quote="removed the old API"):
    return {"id": f"pypi:x:{v}", "version": v, "date": date, "flagged": flagged, "prerelease": False,
            "quotes": [{"match": "zzz", "quote": quote}] if flagged else []}


class Across(unittest.TestCase):
    def test_order_and_anchors(self):
        p = {"releases": [rec("2.0.0", "2026-11-20T00:00:00+00:00", True, "removed B"), rec("2.0.1", "2026-11-21T00:00:00+00:00", False)],
             "history": [rec("1.0.0+x y", "2026-10-01T00:00:00+00:00", True, "removed A")], "history_since": "2026-09-01T00:00:00+00:00"}
        h = build.flagged_across_versions(p)
        self.assertIn("Flagged across versions (since 2026-09-01)", h)
        self.assertLess(h.index("removed A"), h.index("removed B"))
        self.assertNotIn("2.0.1", h)
        self.assertIn('href="#v1.0.0-x-y"', h)

    def test_anchor(self):
        self.assertEqual(build.version_anchor("1.2.3-rc.1+b@x"), "v1.2.3-rc.1-b-x")

    def test_empty(self):
        p = {"releases": [rec("1.0", "2026-11-20T00:00:00+00:00", False)], "history": [], "history_since": "2026-09-01T00:00:00+00:00"}
        self.assertIn("No flagged releases in our history since 2026-09-01", build.flagged_across_versions(p))


if __name__ == "__main__":
    unittest.main()

"""T-0176: bump kind on cards and in pkg index.json."""
import sys, unittest
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import build


def r(v, prev, bump, flagged=True):
    return {"version": v, "prev_version": prev, "bump": bump, "prerelease": False, "flagged": flagged}


class BumpKind(unittest.TestCase):
    def test_patch(self):
        x = r("1.2.4", "1.2.3", "patch")
        self.assertEqual(build.bump_kind(x), "patch")
        self.assertIn("Keyword match in a patch release (1.2.3 → 1.2.4)", build.bump_step_line(x))

    def test_minor(self):
        x = r("1.3.0", "1.2.3", "minor")
        self.assertEqual(build.bump_kind(x), "minor")
        self.assertIn("Keyword match in a minor release (1.2.3 → 1.3.0)", build.bump_step_line(x))

    def test_zero_x(self):
        self.assertEqual(build.bump_kind(r("0.4.1", "0.4.0", "patch")), "0.x")

    def test_major_no_mark(self):
        x = r("2.0.0", "1.9.0", "major")
        self.assertEqual(build.bump_kind(x), "major")
        self.assertEqual(build.bump_step_line(x), "")

    def test_unflagged_no_mark(self):
        self.assertEqual(build.bump_step_line(r("1.2.4", "1.2.3", "patch", False)), "")

    def test_no_previous(self):
        self.assertIsNone(build.bump_kind(r("1.0.0", None, "unknown")))


if __name__ == "__main__":
    unittest.main()

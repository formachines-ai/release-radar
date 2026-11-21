"""T-0173: followed_by = {version, date, hours} for a later same-line patch within 7 days, none otherwise."""
import sys, unittest
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import build


def rec(v, d):
    return {"id": "pypi:x:" + v, "version": v, "date": d, "prerelease": False}


class FollowedBy(unittest.TestCase):
    def test_follow_up(self):
        pkg = {"releases": [rec("1.2.3", "2026-11-10T10:00:00+00:00"), rec("1.2.4", "2026-11-10T15:30:00+00:00")], "history": []}
        build.mark_followed_by(pkg)
        self.assertEqual(pkg["releases"][0]["followed_by"], {"version": "1.2.4", "date": "2026-11-10", "hours": 5})
        self.assertNotIn("followed_by", pkg["releases"][1])
        self.assertEqual(build.followed_text(pkg["releases"][0]["followed_by"]), "Followed by 1.2.4 on 2026-11-10 (5 h later)")

    def test_no_follow_up(self):
        pkg = {"releases": [rec("1.2.3", "2026-11-01T10:00:00+00:00"), rec("1.2.4", "2026-11-10T10:00:00+00:00"),
                            rec("1.3.0", "2026-11-01T12:00:00+00:00")], "history": []}
        build.mark_followed_by(pkg)
        self.assertNotIn("followed_by", pkg["releases"][0])


if __name__ == "__main__":
    unittest.main()

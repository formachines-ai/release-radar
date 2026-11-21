"""T-0170: every public/go/**/index.html carries robots noindex,nofollow and a non-empty redirect target.
Also checks that build.normalise_legacy_go repairs a drifted legacy page without changing its target."""
import re, sys, tempfile, unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import build

NOINDEX = '<meta name="robots" content="noindex,nofollow">'


class GoPages(unittest.TestCase):
    def test_public_go_pages(self):
        pages = list((ROOT / "public" / "go").rglob("index.html"))
        self.assertTrue(pages)
        for p in pages:
            t = p.read_text(encoding="utf-8")
            self.assertIn(NOINDEX, t.split("</head>")[0], str(p))
            m = re.search(r'http-equiv="refresh" content="0;url=([^"]+)"', t)
            self.assertTrue(m and m.group(1).strip(), str(p))

    def test_normalise_in_temp_dir(self):
        with tempfile.TemporaryDirectory() as d:
            old_pub = build.PUBLIC_DIR
            build.PUBLIC_DIR = Path(d)
            try:
                for rel, vote in (("go/abc/index.html", False), ("go/vote/x/up/index.html", True)):
                    p = Path(d) / rel
                    p.parent.mkdir(parents=True)
                    html_ = build.render_go("https://example.com/a?b=1&c=2", "2026-01-01T00:00:00Z", vote=vote)
                    p.write_text(html_.replace("noindex,nofollow", "noindex, nofollow"), encoding="utf-8")
                build.normalise_legacy_go("2026-11-21T00:00:00Z")
                for p in Path(d).rglob("index.html"):
                    t = p.read_text(encoding="utf-8")
                    self.assertIn(NOINDEX, t)
                    self.assertIn('url=https://example.com/a?b=1&amp;c=2"', t)
            finally:
                build.PUBLIC_DIR = old_pub


if __name__ == "__main__":
    unittest.main()

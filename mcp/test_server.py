"""Run: python3 mcp/test_server.py  (offline: serves ../public over file://-like local http)."""
import functools, http.server, json, os, subprocess, sys, threading, unittest

HERE = os.path.dirname(os.path.abspath(__file__))
PUBLIC = os.path.join(HERE, "..", "public")


class Q(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *a): pass


class T(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        h = functools.partial(Q, directory=PUBLIC)
        cls.srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), h)
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()
        cls.base = f"http://127.0.0.1:{cls.srv.server_port}"

    @classmethod
    def tearDownClass(cls): (cls.srv.shutdown(), cls.srv.server_close())

    def rpc(self, *msgs):
        inp = "\n".join(json.dumps(m) for m in msgs) + "\n"
        p = subprocess.run([sys.executable, os.path.join(HERE, "server.py")], input=inp, capture_output=True, text=True,
                           env={**os.environ, "RELEASE_RADAR_BASE": self.base}, timeout=60)
        return [json.loads(l) for l in p.stdout.splitlines()]

    def call(self, name, args):
        r = self.rpc({"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": name, "arguments": args}})[0]["result"]
        return r, (None if r.get("isError") else json.loads(r["content"][0]["text"]))

    def test_list(self):
        r = self.rpc({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
                     {"jsonrpc": "2.0", "method": "notifications/initialized"},
                     {"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
        self.assertEqual(len(r), 2)
        self.assertEqual({t["name"] for t in r[1]["result"]["tools"]}, {"releases_today", "package_releases", "flagged_breaking"})

    def test_today_count_matches(self):
        today = json.load(open(os.path.join(PUBLIC, "today.json")))
        _, res = self.call("releases_today", {"limit": 200})
        self.assertEqual(res["count"], today["counts"]["releases"])
        self.assertEqual(res["count"], len(today["releases"]))

    def test_flagged(self):
        today = json.load(open(os.path.join(PUBLIC, "today.json")))
        _, res = self.call("flagged_breaking", {"limit": 200})
        self.assertEqual(res["count"], today["counts"]["flagged"])

    def test_package_and_missing(self):
        _, res = self.call("package_releases", {"ecosystem": "pypi", "name": "anthropic"})
        self.assertEqual(res["package"], "anthropic")
        r, _ = self.call("package_releases", {"ecosystem": "pypi", "name": "no-such-pkg-xyz"})
        self.assertTrue(r["isError"])


if __name__ == "__main__":
    unittest.main()

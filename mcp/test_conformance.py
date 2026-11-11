"""Conformance session over stdio against the live site. Run: python3 mcp/test_conformance.py
Starts release_radar_mcp.py as a subprocess, speaks newline-delimited JSON-RPC like a real MCP client."""
import json, os, subprocess, sys, unittest, urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
LIVE = os.environ.get("RELEASE_RADAR_BASE", "https://releases.formachines.ai").rstrip("/")


class Client:
    def __init__(self):
        self.p = subprocess.Popen([sys.executable, os.path.join(HERE, "release_radar_mcp.py")], stdin=subprocess.PIPE,
                                  stdout=subprocess.PIPE, text=True, env={**os.environ, "RELEASE_RADAR_BASE": LIVE})
        self.n = 0

    def send(self, method, params=None, notify=False):
        m = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            m["params"] = params
        if not notify:
            self.n += 1
            m["id"] = self.n
        self.p.stdin.write(json.dumps(m) + "\n"); self.p.stdin.flush()
        if notify:
            return None
        r = json.loads(self.p.stdout.readline())
        assert r["jsonrpc"] == "2.0" and r["id"] == self.n, r
        return r

    def tool(self, name, args):
        return self.send("tools/call", {"name": name, "arguments": args})

    def close(self):
        self.p.stdin.close(); self.p.wait(timeout=10)
        return self.p.returncode


def payload(r):
    assert "result" in r and not r["result"].get("isError"), r
    c = r["result"]["content"]
    assert c[0]["type"] == "text"
    return json.loads(c[0]["text"])


class Session(unittest.TestCase):
    def test_full_session(self):
        c = Client()
        # initialize: negotiation
        r = c.send("initialize", {"protocolVersion": "2025-03-26", "capabilities": {}, "clientInfo": {"name": "t", "version": "1"}})
        res = r["result"]
        self.assertEqual(res["protocolVersion"], "2025-03-26")          # supported version is echoed
        self.assertIn("tools", res["capabilities"])
        self.assertTrue(res["serverInfo"]["name"] and res["serverInfo"]["version"])
        r = c.send("initialize", {"protocolVersion": "1999-01-01", "capabilities": {}})
        self.assertIn(r["result"]["protocolVersion"], ["2025-06-18", "2025-03-26", "2024-11-05"])  # server's own
        c.send("notifications/initialized", notify=True)                 # must produce no response
        self.assertEqual(c.send("ping")["result"], {})
        # tools/list
        tools = c.send("tools/list")["result"]["tools"]
        self.assertEqual({t["name"] for t in tools}, {"releases_today", "get_package_releases", "list_flagged"})
        for t in tools:
            self.assertTrue(t["description"])
            s = t["inputSchema"]
            self.assertEqual(s["type"], "object")
            self.assertIsInstance(s["properties"], dict)
            self.assertTrue(set(s.get("required", [])) <= set(s["properties"]))
            for p in s["properties"].values():
                self.assertIn(p["type"], ("string", "integer", "boolean", "number", "object", "array"))
        # tools/call vs live site
        live = json.load(urllib.request.urlopen(urllib.request.Request(LIVE + "/today.json", headers={"User-Agent": "rr-conf"}), timeout=20))
        today = payload(c.tool("releases_today", {"limit": 5}))
        self.assertGreater(today["count"], 0)
        self.assertEqual(today["count"], live["counts"]["releases"])
        self.assertEqual(today["count"], len(live["releases"]))
        self.assertLessEqual(len(today["releases"]), 5)
        fl = payload(c.tool("list_flagged", {}))
        for x in fl["releases"]:
            self.assertTrue(x["flagged"])
        pk = payload(c.tool("get_package_releases", {"ecosystem": "pypi", "name": "anthropic"}))
        self.assertEqual(pk["package"], "anthropic")
        bad = c.tool("get_package_releases", {"ecosystem": "pypi", "name": "no-such-pkg-xyz"})
        self.assertTrue(bad["result"]["isError"])
        # errors
        r = c.tool("no_such_tool", {})
        self.assertIn("error", r); self.assertEqual(r["error"]["code"], -32602)
        r = c.send("no/such/method")
        self.assertEqual(r["error"]["code"], -32601)
        self.assertEqual(c.send("ping")["result"], {})                    # still alive
        self.assertEqual(c.close(), 0)


if __name__ == "__main__":
    unittest.main()

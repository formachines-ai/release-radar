"""Run: python3 -m unittest mcp/test_release_radar_mcp.py  (fixtures, no network)."""
import importlib.util, json, os, unittest
from unittest import mock

spec = importlib.util.spec_from_file_location("rrm", os.path.join(os.path.dirname(os.path.abspath(__file__)), "release_radar_mcp.py"))
m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)

Q = "Removed the legacy `client.foo` API."
REL = lambda v, d, f, eco="pypi": {"package": "p" + v, "ecosystem": eco, "version": v, "date": d, "flagged": f,
                                   "quotes": [Q] if f else [], "notes_url": "https://x/" + v, "errata_url": None, "extra": 1}
TODAY = {"built": "2026-11-11T06:00:03Z", "releases": [
    REL("1", "2026-11-10T01:00:00Z", True), REL("2", "2026-11-10T01:00:00Z", False),
    REL("3", "2026-11-06T01:00:00Z", True, "npm"), REL("4", "2026-11-04T07:00:00Z", True)]}
PKG = {"package": "openai", "ecosystem": "pypi", "built": "b", "latest": "3", "errata": None, "page": "pg",
       "releases": [REL("3", "2026-11-10T01:00:00Z", True)]}


def fake(path):
    if path == "/today.json": return TODAY
    if path == "/pkg/pypi/openai/index.json": return PKG
    raise AssertionError(path)


def rpc(msg):
    with mock.patch.object(m, "fetch", fake):
        return m.handle({"jsonrpc": "2.0", "id": 1, **msg})


def call(name, args):
    r = rpc({"method": "tools/call", "params": {"name": name, "arguments": args}})["result"]
    return r, (json.loads(r["content"][0]["text"]) if not r.get("isError") else r["content"][0]["text"])


class T(unittest.TestCase):
    def test_tools_listed(self):
        names = [t["name"] for t in rpc({"method": "tools/list"})["result"]["tools"]]
        self.assertEqual(names, ["get_package_releases", "list_flagged"])

    def test_list_flagged_default(self):
        _, d = call("list_flagged", {})
        self.assertEqual([r["version"] for r in d["releases"]], ["1", "3", "4"])
        self.assertEqual(d["releases"][0]["quotes"], [Q])
        self.assertNotIn("extra", d["releases"][0])

    def test_list_flagged_days_and_eco(self):
        _, d = call("list_flagged", {"days": 2})
        self.assertEqual([r["version"] for r in d["releases"]], ["1"])
        _, d = call("list_flagged", {"ecosystem": "npm"})
        self.assertEqual([r["version"] for r in d["releases"]], ["3"])

    def test_days_limit(self):
        r, _ = call("list_flagged", {"days": 8})
        self.assertTrue(r["isError"])

    def test_package(self):
        _, d = call("get_package_releases", {"ecosystem": "pypi", "name": "OpenAI"})
        self.assertEqual(d["releases"][0]["quotes"], [Q])

    def test_bad_args(self):
        r, _ = call("get_package_releases", {"ecosystem": "gem", "name": "x"})
        self.assertTrue(r["isError"])

    def test_stdlib_only(self):
        import ast
        mods = set()
        for n in ast.walk(ast.parse(open(m.__file__).read())):
            if isinstance(n, ast.Import): mods |= {x.name.split(".")[0] for x in n.names}
            elif isinstance(n, ast.ImportFrom): mods.add(n.module.split(".")[0])
        self.assertLessEqual(mods, {"datetime", "json", "os", "sys", "urllib"})


if __name__ == "__main__":
    unittest.main()

#!/usr/bin/env python3
"""Release Radar MCP server (read-only, stdio). Python stdlib only.

Reads the public JSON of https://releases.formachines.ai and exposes three tools:
releases_today, package_releases, flagged_breaking. Quotes are returned verbatim
from the site; nothing is summarised or judged here.
Built by AI agents (agent-team) without human editing; responsible: Dmitry, formachines.ai.
"""
import json
import os
import sys
import urllib.parse
import urllib.request

BASE = os.environ.get("RELEASE_RADAR_BASE", "https://releases.formachines.ai").rstrip("/")
VERSION = "0.1.0"
PROTOCOL = "2024-11-05"
TIMEOUT = 15


def fetch(path: str):
    url = BASE + path
    req = urllib.request.Request(url, headers={"User-Agent": f"release-radar-mcp/{VERSION}"})
    with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
        return json.loads(r.read().decode("utf-8"))


def _filter(rels, eco=None, stable_only=False, flagged_only=False, limit=50):
    out = []
    for r in rels:
        if eco and r.get("ecosystem") != eco:
            continue
        if stable_only and r.get("prerelease"):
            continue
        if flagged_only and not r.get("flagged"):
            continue
        out.append(r)
    return out[: max(1, min(int(limit or 50), 200))]


def releases_today(args):
    d = fetch("/today.json")
    allr = d.get("releases", [])
    rels = _filter(allr, args.get("ecosystem"), args.get("stable_only", False), False, args.get("limit", 50))
    return {"built": d.get("built"), "window_days": d.get("window_days"),
            "count": d.get("counts", {}).get("releases", len(allr)), "returned": len(rels),
            "releases": rels, "source": BASE + "/today.json", "disclosure": d.get("disclosure")}


def flagged_breaking(args):
    d = fetch("/today.json")
    rels = _filter(d.get("releases", []), args.get("ecosystem"), args.get("stable_only", False), True, args.get("limit", 50))
    return {"built": d.get("built"), "count": len(rels), "releases": rels,
            "note": "Flag = regex match in release notes (breaking/deprecated/removed/renamed/dropped support/no longer); quotes are verbatim.",
            "source": BASE + "/today.json"}


def package_releases(args):
    eco, name = args.get("ecosystem"), args.get("name")
    if eco not in ("pypi", "npm") or not name or not isinstance(name, str):
        raise ValueError("ecosystem must be 'pypi' or 'npm' and name a package name")
    path = "/pkg/%s/%s/index.json" % (eco, urllib.parse.quote(name.lower() if eco == "pypi" else name, safe="@/"))
    try:
        return fetch(path)
    except urllib.error.HTTPError as e:
        if e.code == 404:
            raise ValueError(f"package {eco}:{name} is not tracked by Release Radar")
        raise


_ECO = {"type": "string", "enum": ["pypi", "npm"]}
TOOLS = {
    "releases_today": (releases_today, "Releases of the tracked PyPI/npm packages in the site's 7-day window (from today.json), newest first, with breaking-flag quotes and links.",
                       {"ecosystem": _ECO, "stable_only": {"type": "boolean"}, "limit": {"type": "integer", "minimum": 1, "maximum": 200}}, []),
    "package_releases": (package_releases, "Releases and metadata of one tracked package (from /pkg/<eco>/<name>/index.json).",
                         {"ecosystem": _ECO, "name": {"type": "string", "description": "e.g. requests or @anthropic-ai/sdk"}}, ["ecosystem", "name"]),
    "flagged_breaking": (flagged_breaking, "Releases whose notes match the breaking/deprecated/removed regex, each with the verbatim quote and notes link. Regex match only, no judgement.",
                         {"ecosystem": _ECO, "stable_only": {"type": "boolean"}, "limit": {"type": "integer", "minimum": 1, "maximum": 200}}, []),
}


def tool_list():
    return [{"name": n, "description": d, "annotations": {"readOnlyHint": True},
             "inputSchema": {"type": "object", "properties": p, "required": req}} for n, (_, d, p, req) in TOOLS.items()]


def handle(msg):
    """Return a response dict, or None for notifications."""
    mid, method, params = msg.get("id"), msg.get("method"), msg.get("params") or {}
    if mid is None:
        return None
    def ok(result): return {"jsonrpc": "2.0", "id": mid, "result": result}
    def err(code, text): return {"jsonrpc": "2.0", "id": mid, "error": {"code": code, "message": text}}
    if method == "initialize":
        return ok({"protocolVersion": PROTOCOL, "capabilities": {"tools": {}},
                   "serverInfo": {"name": "release-radar-mcp", "version": VERSION}})
    if method == "ping":
        return ok({})
    if method == "tools/list":
        return ok({"tools": tool_list()})
    if method == "tools/call":
        name = params.get("name")
        if name not in TOOLS:
            return err(-32602, f"unknown tool {name}")
        try:
            res = TOOLS[name][0](params.get("arguments") or {})
            return ok({"content": [{"type": "text", "text": json.dumps(res, ensure_ascii=False)}]})
        except Exception as e:  # tool errors are reported in-band
            return ok({"isError": True, "content": [{"type": "text", "text": f"{type(e).__name__}: {e}"}]})
    return err(-32601, f"method not found: {method}")


def main():
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except ValueError:
            out = {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "parse error"}}
        else:
            out = handle(msg) if isinstance(msg, dict) else None
        if out is not None:
            sys.stdout.write(json.dumps(out, ensure_ascii=False) + "\n")
            sys.stdout.flush()


if __name__ == "__main__":
    main()

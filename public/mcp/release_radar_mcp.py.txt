#!/usr/bin/env python3
"""Release Radar MCP server (T-0106) (read-only, stdio). Python stdlib only.

Reads only the public JSON of https://releases.formachines.ai and exposes three tools:
releases_today, get_package_releases, list_flagged. Quotes are returned verbatim
from the site; nothing is summarised or judged here.
Built by AI agents (agent-team) without human editing; responsible: Dmitry, formachines.ai.
"""
import datetime
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request

BASE = os.environ.get("RELEASE_RADAR_BASE", "https://releases.formachines.ai").rstrip("/")
VERSION = "0.1.0"
SUPPORTED = ["2025-06-18", "2025-03-26", "2024-11-05"]  # newest first
PROTOCOL = SUPPORTED[0]
TIMEOUT = 15


def fetch(path: str):
    url = BASE + path
    req = urllib.request.Request(url, headers={"User-Agent": f"release-radar-mcp/{VERSION}"})
    with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
        return json.loads(r.read().decode("utf-8"))


FIELDS = ("package", "ecosystem", "version", "date", "flagged", "quotes", "notes_url", "notes_redirect", "errata_url", "page")


def _pick(r):
    return {k: r.get(k) for k in FIELDS if k in r}


def _parse(ts):
    return datetime.datetime.strptime(ts, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=datetime.timezone.utc)


def list_flagged(args):
    days = args.get("days", 7)
    if isinstance(days, bool) or not isinstance(days, int) or not 1 <= days <= 7:
        raise ValueError("days must be an integer from 1 to 7")
    eco = args.get("ecosystem")
    if eco is not None and eco not in ("pypi", "npm"):
        raise ValueError("ecosystem must be 'pypi' or 'npm'")
    d = fetch("/today.json")
    cutoff = _parse(d["built"]) - datetime.timedelta(days=days)
    rels = [_pick(r) for r in d.get("releases", [])
            if r.get("flagged") and (not eco or r.get("ecosystem") == eco) and _parse(r["date"]) >= cutoff]
    return {"built": d.get("built"), "days": days, "count": len(rels), "releases": rels, "source": BASE + "/today.json"}


def releases_today(args):
    eco, limit = args.get("ecosystem"), args.get("limit", 50)
    if eco is not None and eco not in ("pypi", "npm"):
        raise ValueError("ecosystem must be 'pypi' or 'npm'")
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 200:
        raise ValueError("limit must be an integer from 1 to 200")
    d = fetch("/today.json")
    allr = d.get("releases", [])
    rels = [_pick(r) for r in allr if (not eco or r.get("ecosystem") == eco)
            and not (args.get("stable_only") and r.get("prerelease"))][:limit]
    return {"built": d.get("built"), "window_days": d.get("window_days"),
            "count": d.get("counts", {}).get("releases", len(allr)), "returned": len(rels),
            "releases": rels, "source": BASE + "/today.json"}


def get_package_releases(args):
    eco, name = args.get("ecosystem"), args.get("name")
    if eco not in ("pypi", "npm") or not name or not isinstance(name, str):
        raise ValueError("ecosystem must be 'pypi' or 'npm' and name a package name")
    path = "/pkg/%s/%s/index.json" % (eco, urllib.parse.quote(name.lower() if eco == "pypi" else name, safe="@/"))
    try:
        d = fetch(path)
    except urllib.error.HTTPError as e:
        if e.code == 404:
            raise ValueError(f"package {eco}:{name} is not tracked by Release Radar")
        raise
    return {"package": d.get("package"), "ecosystem": d.get("ecosystem"), "built": d.get("built"), "latest": d.get("latest"),
            "errata": d.get("errata"), "page": d.get("page"), "source": BASE + path,
            "releases": [_pick(r) for r in d.get("releases", [])]}


_ECO = {"type": "string", "enum": ["pypi", "npm"]}
TOOLS = {
    "releases_today": (releases_today, "Releases of the tracked PyPI/npm packages in the site's 7-day window (from /today.json), newest first, with flag, verbatim quotes and links.",
                       {"ecosystem": _ECO, "stable_only": {"type": "boolean"}, "limit": {"type": "integer", "minimum": 1, "maximum": 200}}, []),
    "get_package_releases": (get_package_releases, "What was flagged in one tracked package: its releases in the site's 7-day window with version, date, flag, verbatim quotes, notes link and Errata link (from /pkg/<eco>/<name>/index.json). Regex flag only, no judgement.",
                             {"ecosystem": _ECO, "name": {"type": "string", "description": "e.g. requests or @anthropic-ai/sdk"}}, ["ecosystem", "name"]),
    "list_flagged": (list_flagged, "Flagged releases (notes match the breaking/deprecated/removed regex) of the last N days (1-7) across tracked packages, with verbatim quotes and links (from /today.json).",
                     {"days": {"type": "integer", "minimum": 1, "maximum": 7}, "ecosystem": _ECO}, []),
}


def tool_list():
    return [{"name": n, "description": d, "annotations": {"readOnlyHint": True},
             "inputSchema": {"type": "object", "properties": p, "required": req}} for n, (_, d, p, req) in TOOLS.items()]


def handle(msg):
    """Return a response dict, or None for notifications."""
    mid, method, params = msg.get("id"), msg.get("method"), msg.get("params") or {}
    if not isinstance(params, dict):
        params = {}
    if mid is None:
        return None
    def ok(result): return {"jsonrpc": "2.0", "id": mid, "result": result}
    def err(code, text): return {"jsonrpc": "2.0", "id": mid, "error": {"code": code, "message": text}}
    if method == "initialize":
        want = params.get("protocolVersion")
        return ok({"protocolVersion": want if want in SUPPORTED else PROTOCOL, "capabilities": {"tools": {}},
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

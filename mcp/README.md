# release-radar-mcp

Read-only MCP server (stdio) over the public JSON of [releases.formachines.ai](https://releases.formachines.ai).
Python 3.10+ standard library only, no install step. Built by AI agents (agent-team) without human editing;
responsible: Dmitry, formachines.ai.

## Install

Claude Code:

```bash
claude mcp add release-radar -- python3 /path/to/release-radar/mcp/server.py
```

Any client (Cursor, Codex …), JSON config:

```json
{"mcpServers": {"release-radar": {"command": "python3", "args": ["/path/to/release-radar/mcp/server.py"]}}}
```

(npm / MCP-registry packaging is a follow-up task.)

## Tools

| Tool | Arguments | Returns |
|---|---|---|
| `releases_today` | `ecosystem?` (`pypi`/`npm`), `stable_only?`, `limit?` (≤200) | releases in the 7-day window from `/today.json`, plus `count` (same as `counts.releases`) |
| `package_releases` | `ecosystem`, `name` | `/pkg/<eco>/<name>/index.json` of one tracked package |
| `flagged_breaking` | `ecosystem?`, `stable_only?`, `limit?` | releases whose notes match the breaking regex, with the verbatim quote and notes link |

The flag is a regex match, not a judgement; quotes are verbatim. No data leaves your machine except the GET requests
to the site. `RELEASE_RADAR_BASE` overrides the base URL.

## Test

```bash
python3 mcp/test_server.py        # serves ./public locally, no network
python3 mcp/test_conformance.py   # full stdio session against the live site (needs network)
```

## Conformance

- **Protocol versions:** the server answers `initialize` with the client's `protocolVersion` if it is one of
  `2025-06-18`, `2025-03-26`, `2024-11-05`, otherwise with `2025-06-18`. Checked by the test with `2025-03-26` and an unknown version.
- **Result (2026-11-03):** `python3 mcp/test_conformance.py` passes against https://releases.formachines.ai. Session:
  initialize + negotiation, `notifications/initialized` (no reply), `ping`, `tools/list` (name, description, object
  `inputSchema` for all three tools), `tools/call` for all three tools (`releases_today` count equals `counts.releases` of the
  live `today.json`), unknown method → `-32601`, unknown tool → JSON-RPC error `-32602` (as the spec prescribes; the server keeps
  running), tool failures (untracked package) → `result.isError: true`. Fixed during the check: `initialize` always answered
  `2024-11-05` instead of negotiating; non-object `params` could crash the handler.
  Not covered: a real Claude Code/Cursor connection, JSON-RPC batches, `resources`/`prompts` (not offered), cancellation, progress.
- **Config** (Claude Code `.mcp.json` / Cursor `mcp.json`):

```json
{"mcpServers": {"release-radar": {"command": "python3", "args": ["/path/to/release-radar/mcp/server.py"]}}}
```

- **SDK recommendation:** publish the current stdlib server as is for the first release: it exposes only three read-only
  tools, passes the scripted session, and has no dependencies (nothing to install beyond Python). Switch to the official MCP SDK
  only if we add resources/prompts, structured output, streamable HTTP or auth, or if a real client reports an incompatibility;
  for an npm package the official TypeScript SDK would be the natural choice then, at the cost of a dependency and a rewrite.
  Before publishing, do one manual check with real Claude Code (`claude mcp add …`, call each tool).

## Single-file server (T-0106): `release_radar_mcp.py`

`release_radar_mcp.py` is the standalone stdio server published at
<https://releases.formachines.ai/mcp/release_radar_mcp.py>. Stdlib only; reads only `releases.formachines.ai` JSON.

| Tool | Arguments | Returns |
|---|---|---|
| `get_package_releases` | `ecosystem` (`pypi`/`npm`), `name` | the package's releases from `/pkg/<eco>/<name>/index.json`: version, date, flag, verbatim quotes, notes link, Errata link |
| `list_flagged` | `days` (1–7, default 7), `ecosystem?` | flagged releases from `/today.json` of the last `days` days, same fields |

Claude Code: `claude mcp add release-radar -- python3 /path/to/release_radar_mcp.py`
Cursor (`.cursor/mcp.json`): `{"mcpServers": {"release-radar": {"command": "python3", "args": ["/path/to/release_radar_mcp.py"]}}}`

Test: `python3 -m unittest mcp/test_release_radar_mcp.py` (fixtures, no network).

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
python3 mcp/test_server.py   # serves ./public locally, no network
```

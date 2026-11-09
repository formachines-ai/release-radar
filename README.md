# Release Radar — `releases.formachines.ai`

A daily, human-readable feed of PyPI and npm releases for ~100 tracked packages: package · version · date · a flag
when the release notes contain *breaking / deprecated / removed / renamed / dropped support / no longer* — with the
**verbatim** 1–2 line quote from the notes — a link to the release notes, and a badge when the package has a
sandbox-verified correction on [formachines.ai/errata](https://formachines.ai/errata/).

The site is static HTML + one CSS file. No JavaScript, no cookies, no forms, no analytics. It is built and
published by an autonomous team of AI agents (agent-team); the responsible person is Dmitry, formachines.ai.
This README is the contract for the `developer` role: read it fully before changing anything.

## Build

```bash
python3 build.py                     # full build: registries + GitHub Releases + Errata index → public/
python3 build.py --offline           # rebuild from data/ caches, no network (for template work)
python3 build.py --no-github         # registries only; release notes become fallback links
python3 build.py --limit 5 -v        # smoke test on the first 5 packages, debug log
GITHUB_TOKEN=ghp_… python3 build.py  # recommended: 5 000 GitHub requests/h instead of 60
```

- **Python 3.12 standard library only** (3.10+ runs it): `urllib`, `json`, `re`, `html`, `datetime`, `pathlib`,
  `gzip`, `hashlib`, `logging`, `argparse`. **No pip, no third-party imports, no npm.** Adding a dependency is a
  defect, not a task.
- The build is a single file, `build.py`. All tunable values (window, timeouts, limits, URLs, the breaking regex)
  are constants at the top of the file.
- Network calls: `https://pypi.org/pypi/<pkg>/json`, `https://registry.npmjs.org/<pkg>`, `https://api.github.com/
  repos/<owner>/<repo>/releases` (only for repos that had a release in the window), and
  `https://formachines.ai/errata/index.json`. Timeout 15 s, two retries; a failed request is logged, the cached copy in
  `data/` is used if present, otherwise the package is skipped. A build never aborts because one source is down.
- `GITHUB_TOKEN` (read-only public token) is taken from the environment if present. Without it GitHub allows
  60 requests/hour per IP; the build stops calling GitHub when the remaining quota reaches 2, logs a warning, and
  the affected releases get a fallback link (changelog URL, `…/releases` page or the registry page) and
  `notes_kind != "github_release"`. They are not flagged, because nothing was read.
- A full build takes about one minute. The log goes to stderr with timestamps; the last line is the summary
  (`packages=… releases=… flagged=… errors=…`). `data/state.json` keeps the last summary.

## What to commit

**`public/` is committed.** The server only publishes the committed directory (it pulls the repo every 5 minutes
and swaps a symlink). It does not build, has no Python packages, no Node. Changing a template without rebuilding and
committing `public/` changes nothing on the site. `data/` is committed too (small, trimmed JSON caches — see below).

After any change: `python3 build.py` → open the generated files → `git add -A` → commit with the task id first in
the message. The dispatcher merges task branches into `main`; never push from a role run.

## Layout

```
build.py                    the generator (single file, stdlib)
config/packages.txt         tracked packages: one `pypi:<name>` or `npm:<name>` per line, # comments
config/ignore_patterns.txt  regexes that cancel a breaking-change match (false-positive guard), one per line
static/style.css            the only stylesheet; copied to public/style.css verbatim
data/registry/pypi/<pkg>.json   trimmed registry metadata (newest versions + dates, repo, changelog link)
data/registry/npm/<pkg>.json    same for npm (`time` map trimmed to 90 days; the raw document can be tens of MB)
data/github/<owner>__<repo>.json  newest GitHub releases of a repo: tag, url, date, body (≤ 60 000 chars)
data/errata/index.json      last successful copy of the Errata package index
data/state.json             summary of the last build
public/                     the site — everything below is generated, do not edit by hand
  index.html                front page: header stats, last 24 h (48 h on a quiet day), flagged, PyPI, npm, all packages
  pkg/<eco>/<name>/index.html   package page   (scoped npm packages keep the scope: pkg/npm/@scope/name/)
  pkg/<eco>/<name>/index.json   package data
  pkg/<eco>/<name>/feed.xml     per-package RSS 2.0 (that package's releases in the window; valid empty channel if none)
  today.json                every release in the window with quotes and links
  feed.xml                  RSS 2.0 (all releases in the window, newest first)
  feed-breaking.xml         RSS 2.0, only flagged releases of the window (T-0073): package@version, date, ≤ 3 verbatim quotes, /go/<id>/ notes link; valid empty channel if none flagged; linked from the home Follow line, <head> and llms.txt
  llms.txt · robots.txt · sitemap.xml · build.json
  ci-check.py              CI helper script, copied from static/ci-check.py (T-0074)
  ci-check.txt             byte-identical copy of ci-check.py: nginx serves .py as application/octet-stream (a download), .txt as text/plain, so readers can inspect it first (T-0075)
  go/<id>/index.html        meta-refresh redirect to an external URL; id = sha1(url)[:10]; go/index.json maps ids → URLs
  legal/index.html          responsible person, data sources, license, no-tracking statement
  404.html                  with navigation; nginx serves it for unknown paths
  style.css
```

## Rules the code must keep (promises in VISION.md)

1. **Addresses never change.** `/`, `/pkg/<eco>/<name>/`, `/pkg/<eco>/<name>/index.json`, `/pkg/<eco>/<name>/feed.xml`, `/today.json`, `/feed.xml`,
   `/llms.txt`, `/go/<id>/`, `/legal/` are stable. Removing a package from `config/packages.txt` removes its page —
   prefer not to; `build.py` never deletes `public/` wholesale.
2. **AI disclosure on every page.** The footer line
   `Built by AI agents (agent-team) without human editing · responsible: Dmitry, formachines.ai · updated <UTC>`
   is produced by `layout()` and `render_go()`; every HTML file must contain it. `today.json`, `llms.txt` and the RSS
   channel carry the same sentence.
3. **No judgement about "breaking".** The flag is `BREAKING_RE` applied to the release-notes text, nothing else.
   The quote is the matched line (plus the previous line when the matched line is short), cut at 280 characters with
   an ellipsis, never paraphrased. No LLM call, no summary, no "probably breaking". False positives are handled by
   adding a regex to `config/ignore_patterns.txt`, never by editing the quote. Changelogs are not republished:
   at most 3 quotes per release, each ≤ 280 characters, always with the link to the source.
4. **No tracking.** No scripts, cookies, forms, pixels, external fonts or third-party embeds. External clicks go
   through `/go/<id>/` (a static meta-refresh page, `noindex`) so the server log can count them — nothing more.
5. **Quiet day rule.** If no tracked package released in the last 24 h, the "recent" block widens to 48 h and says
   so; the 7-day lists are always rendered. The front page is never empty.
6. **Fail soft.** Every external source may be missing; the build logs it and falls back to cache or a link.

## Adding a package

1. Add one line to `config/packages.txt`: `pypi:<name>` or `npm:<name>` (scoped: `npm:@scope/name`). Keep the
   block sorted.
2. `python3 build.py` — the package gets `data/registry/<eco>/<name>.json`, a page and a JSON; check the log for
   `no metadata` (typo in the name) and the page for a sensible repository link.
3. If the GitHub repository is not detected (page shows no "Repository" row), the registry metadata lacks a GitHub
   URL; release notes fall back to the changelog link or the registry page. That is acceptable; do not hard-code
   repositories in `build.py`.
4. Commit `config/`, `data/` and `public/` together.

Signals for new packages: 404s on `/pkg/<eco>/<name>/` in the server log (see the analyst's metrics) and referers.

## Release-notes coverage (T-0066)

`notes_kind = github_release` means the GitHub release for exactly this version was found. Tags are matched by exact
version only: `1.2.3`, `v1.2.3`, `<name>@1.2.3`, `<name>-v1.2.3`, `@scope/name@1.2.3`, `rust-v1.2.3` (openai/codex), `rel_1_2_3` (sqlalchemy/sqlalchemy; confirmed in its releases list, T-0068), and for
PyPI the PEP 440 spelling of the same pre-release (`1.107.0rc3` = tag `v1.107.0-rc.3`). If the newest-50 list lacks the
tag, the exact candidate tags are requested one by one (404s are cached). No nearby or similar release is ever linked.

- `config/repo_overrides.txt` (optional): `<eco> <name> <owner/repo>` per line, `#` comments. It replaces the repo from
  registry metadata. Add a line only after checking by hand that the repo publishes GitHub Releases for the package
  (e.g. `npm react facebook/react`: the registry says `react/react`).
- `build.json` → `notes_coverage`: `{releases, github_release, missing:[{eco,name,version,reason}]}`. `reason` is one of
  `no_repo_url` (no GitHub repo known), `no_matching_tag` (repo read fine, no release for this exact version: nightlies,
  tag-only repos such as django/boto3, per-platform codex builds), `api_error` (GitHub HTTP error),
  `rate_limited` (403/429 or quota exhausted), `not_fetched` (`--no-github` / `--offline`).
- Grouping (T-0069): when more than 5 `missing` entries share eco+name+reason they are replaced by one
  `{eco,name,reason,count,versions}` entry (`count == len(versions)`); `notes_coverage.releases` still counts every release.
- Coverage at the 08.11 06:00 build: `github_release` 124 of 291 releases (42.6 %) before T-0066, 129 of 291 (44.3 %) after it.
  The remainder are mostly nightly/dev builds and per-platform npm builds that have no GitHub release of their own.

## npm platform builds (T-0069)

npm versions of the form `<base>-(darwin|linux|win32)-(x64|arm64)` (confirmed for `@openai/codex` in `today.json`) get
`variant_of: "<base>"` in `today.json` and `/pkg/*/index.json`; every entry is kept. In the HTML (home and package pages)
unflagged variants whose base version is in the same list fold into the base card as an "N platform builds" line with a
`<details>` list of versions (no JS). Other versions, flagged variants, or variants without a base card stay separate cards.

## Bump label and honest notes on cards

Each release has `bump` (`major|minor|patch|prerelease|unknown`) in `today.json` and `/pkg/*/index.json`, computed by
`compute_bump()`: the version compared with the highest known stable numeric version below it (registry cache).
Pre-releases are `prerelease`; no previous version or a non-numeric version is `unknown` (never guessed).
Cards show the bump; flagged minor/patch releases get "Breaking keyword in a minor release" / "Breaking keyword in a patch release" (text from the `bump` field only; none for major, unknown or unflagged). Unflagged cards say
"No breaking keyword found in the release notes (checked <build date> UTC); notes can omit changes.", or
"Release notes not fetched." when `notes_kind != github_release`. Labels are a version comparison plus the regex only.

## Breaking-change regex

`BREAKING_RE` in `build.py`:

```
\b(?:break(?:ing|s)?|deprecat(?:e|es|ed|ion|ions|ing)|removed|renamed|drop(?:ped|s)?\s+support|no longer)\b
```

case-insensitive, applied line by line to the GitHub release body. A match whose span overlaps a match of any
pattern in `config/ignore_patterns.txt` is dropped (e.g. `line-break`, `breakpoint`). Changing the pattern is a
product decision (VISION.md); extending the ignore list after a confirmed false positive is a normal task.

## Errata badge

`https://formachines.ai/errata/index.json` → `packages[]` with `ecosystem`, `package`, `url`, `json`, `errata`
(count), `updated`, `old_version`, `new_version`. A tracked package present there gets the green badge linking (via
`/go/`) to its Errata page, and the package page shows the checked version pair. Nothing else is derived from
Errata; the Errata pages themselves say on which versions outdated code still reproduces.

## CI check (T-0074)

`static/ci-check.py` (Python 3.12 stdlib, copied by `build.py` to `public/ci-check.py`) reads a `requirements.txt` or
`package.json` (dependencies + devDependencies), fetches `/today.json?src=ci` and prints the dependencies released in the
48 h before `built`: package@version, date, flagged yes/no, verbatim quotes, notes URL. Exit 0 unless `--fail-on-flag`.
A flag is a regex match plus a quote; no flag does not mean safe. GitHub Actions:

```yaml
- run: curl -sO https://releases.formachines.ai/ci-check.py
- run: python3 ci-check.py requirements.txt --fail-on-flag
```

## Checks before reporting a task done

- `python3 build.py` exits 0; the summary line shows `errors=0` or explains each error.
- `python3 -c "import html.parser"`-level sanity: every `public/**/*.html` parses, has `<title>`, the footer
  disclosure, and `<link rel="stylesheet" href="/style.css">`.
- `public/today.json`, `public/build.json`, each `public/pkg/**/index.json` parse as JSON; `public/feed.xml` is
  well-formed XML (`python3 -c "import xml.etree.ElementTree as E; E.parse('public/feed.xml')"`).
- Quotes on the front page exist verbatim in `data/github/<repo>.json` bodies.
- Open `public/index.html` in a browser at 375 px width: no horizontal scroll, table scrolls inside its wrapper.

## Server

Deployed by the agent-team droplet scripts (`deploy/droplet/` in the dispatcher repository): the droplet pulls
`main`, publishes `public/` under nginx for `releases.formachines.ai`, serves `404.html` for unknown paths and
`/_team/metrics.json` from the metrics job. Nothing else runs on the server for this site.

## License

Site text and data files: CC BY 4.0. Quoted release notes remain under their authors' licenses. Code: MIT.

T-0068: prisma needed no change (its repo is prisma/prisma-cli; rc.30–rc.33 already match `v<version>`; the remaining prisma
misses are `-dev.N` npm builds with no GitHub release). Added the `rel_` tag shape for sqlalchemy. Remaining misses (codex
per-platform/alpha builds, playwright/tailwind/typescript/react nightlies, firebase eap, django, boto3 tag-only) have no
GitHub release for that exact version and stay `no_matching_tag`.

## Head tags carry no changing numbers (T-0070)

`description`, `og:*` and `twitter:*` tags on `/` and `/pkg/<eco>/<name>/` are static sentences: no release counts, no
build date (preview bots cache them per URL). Counts and the UTC build time stay in the page body and footer. There
is no `og:image`.

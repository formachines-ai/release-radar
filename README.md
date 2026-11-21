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
                                (T-0158: bottom of the home page, above the footer: section `All tracked packages (P)` with one plain <a> (no nofollow) per sitemap /pkg/ URL, grouped PyPI/npm, sorted by name; href = the sitemap <loc> string, built with page_url())
                                (T-0137: section `Past a 7-day cooldown` below the recent cards: releases 7–8 days old (from /pkg/ history), flagged ones as full cards, the rest as package@version · date · /pkg/ link; one line if the window is empty)
                                (T-0150: home opens, under the <h1>, with a dated `<p class="facts">`: packages (= /pkg/ URLs in sitemap.xml), releases and flagged in the 7 days to build time (= today.json counts), sources; the home meta description carries the same numbers)
  pkg/<eco>/<name>/index.html   package page   (scoped npm packages keep the scope: pkg/npm/@scope/name/)
                                (T-0145: package page opens, under the <h1>, with a dated facts paragraph `<p class="facts">`: releases in 90 days (releases + history), stable, flagged, latest version/date, registry + GitHub Releases links, build date)
                                (T-0149: one `<script type="application/ld+json">` block — a data block, not executable script — WebPage + ItemList of the shown release cards (name, datePublished, notes url), isBasedOn registry/GitHub Releases URL, dateModified = build time; home keeps WebSite + Dataset naming /today.json and /feed.xml; values from build data only)
                                (T-0147: the facts paragraph's `latest` is the page's `Latest version` field (date from that version's record); if the field is missing, the newest non-pre-release, non-platform-build record; pre-releases and platform builds are never named latest)
  pkg/<eco>/<name>/index.json   package data
                                (T-0140: stable, non-platform-build records have `followed_by` {version, hours} when a newer stable version has a later registry date within 168 h; cards show `Followed by <version> after <N> h (registry dates)` (T-0155: wording; rule unchanged: earliest later stable record of the same major.minor with a higher patch within 168 h))
                                (T-0143: the follow-up must be in the same major.minor line and a higher version, earliest such; no such version → no line, no key)
                                (T-0118: `history` key = records older than 7 days up to 90, merged from the previous committed index.json, no re-fetch; `history_since` = first date held)
                                (T-0119: every release record, in `releases` and `history`, has `no_notes` and `no_notes_checked` (build date UTC, null if not checked-empty); history cards restore the "No release notes found … (checked <date>)" label from them)
  pkg/<eco>/<name>/feed.xml     per-package RSS 2.0 (T-0123: that package's releases incl. history, newest first, max 50; flag label + ≤3 verbatim quotes or the no-flag/no-notes line; valid empty channel if none)
  (T-0162: package page <title> = `<name> (<PyPI|npm>) releases and breaking-change quotes – Release Radar`; meta description = name, ecosystem, latest version + UTC date, releases and flagged count in the 90-day history window (or `no release in the last 7 days as of <build date>`), ≤160 chars, data only; supersedes the T-0096/T-0089 title/description wording below)
                                  (T-0166: home, /legal/ and /brand/ have their own <title>; the home meta description is built from build data: packages, 7-day releases, flagged, UTC build date, ≤160 chars)
  (package page <title>, T-0096: first of `<name> <ver> release notes & breaking changes (<eco>) — Release Radar` → without ` — Release Radar` → without `(<eco>)` → `<name> <ver> breaking changes` that is ≤70 chars)
  (package page <title>/meta description, T-0089: built only from the package's data — latest version, its date, flagged count, start of the first verbatim quote ≤160 chars; plus 4 same-ecosystem alphabetical-neighbour links and a link to /)
                                (T-0160: `notes_pr_list_only: true` + card label `Notes are a PR list only (auto-generated)` when the GitHub notes have ≥3 list lines, every list line matches `by @user in #N`/`…/pull/N`, and other lines are only headings, Full Changelog or New Contributors; regex only, no effect on the flag)
                                (T-0161: a card with `notes_pr_list_only` true shows only `Notes are a PR list only (auto-generated)`, not the older `(auto-generated format)` label; JSON unchanged)
                                (T-0146: `notes_format`: `pr_list` in index.json/today.json records and a label `Notes are a PR list (auto-generated format)` on the card when the GitHub notes have ≥3 non-empty bullet lines and ≥80 % end in `by @user in #N` / `…/pull/N` (headings, Full Changelog ignored); build.json `pr_list_notes` counts them; no effect on the breaking flag)
                                (T-0152: records with a known GitHub tag and an earlier stable tag of the same package (same tag prefix, no pre-releases/platform builds) get `compare_url` (github.com/<repo>/compare/<prev_tag>...<tag>) in index.json/today.json and a card link `compare <prev>…<this> →` via `/go/compare/<id>/`; unflagged cards with notes say `No breaking keyword matched in the notes (notes may be incomplete)`; no effect on the flag)
  today.json                every release in the window with quotes and links
  feed.xml                  RSS 2.0 (all releases in the window, newest first)
  feed-breaking.xml         RSS 2.0, only flagged releases of the window (T-0073): package@version, date, ≤ 3 verbatim quotes, /go/<id>/ notes link; valid empty channel if none flagged; linked from the home Follow line, <head> and llms.txt
  llms.txt · robots.txt (T-0151: `User-agent: *` also has an explicit `Disallow: /go/vote/` and /go/ as a whole is NOT disallowed (T-0153: crawlers must be able to read the `noindex, nofollow` meta on every /go/ redirect page; every /go/ link on home and /pkg/ pages carries rel="nofollow"); every vote link carries rel="nofollow") · sitemap.xml · build.json
                            (T-0141, T-0154: llms.txt section `For dependency-review agents (Dependabot/Renovate PRs)`: 3 steps over /pkg/<eco>/<name>/index.json (releases/history, flagged, quotes, notes_url, compare_url, notes_format), example URL, no-flag caveat, /mcp/, limits)
                            (T-0121: sitemap <lastmod> of /pkg/ URLs = date of the package's newest release record shown (7-day list or history), / = build date, /legal/, /brand/ and /mcp/ = fixed dates in build.py; T-0138: /mcp/ listed)
                            (T-0144: one function page_url() writes the absolute page URL for sitemap <loc> and canonical, literal "@" kept in scoped npm paths; every page footer links /brand/)
  mcp/ (index.html, release_radar_mcp.py, .py.txt, README.md; T-0106: stdlib MCP server, sources in mcp/, copied by build.py)
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
   an ellipsis, never paraphrased. (T-0133: a match is widened verbatim to its enclosing bullet/sentence if that fits in 280; otherwise the cut carries `quote_incomplete: true` in JSON and the label «Incomplete excerpt — open the release notes for context» on cards; exact duplicate quotes within a release are dropped.) No LLM call, no summary, no "probably breaking". False positives are handled by
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
`<details>` list of versions · dates (no JS). T-0142: flagged variants fold in too (flag and quotes kept), and variants whose base is not in the list get one `<base> platform builds` group card.

## Bump label and honest notes on cards

Each release has `bump` (`major|minor|patch|prerelease|unknown`) in `today.json` and `/pkg/*/index.json`, computed by
`compute_bump()`: the version compared with the highest known stable numeric version below it (registry cache).
Pre-releases are `prerelease`; no previous version or a non-numeric version is `unknown` (never guessed).
Cards show the bump; flagged minor/patch releases get "Breaking keyword in a minor release" / "Breaking keyword in a patch release" (text from the `bump` field only; none for major, unknown or unflagged). Unflagged cards say
Flagged stable cards with a minor/patch bump and a known previous stable version also get "Flagged in a minor/patch release (A → B)" (or "0.x minor release"); the home shows "K of M flagged releases are minor/patch or 0.x bumps (7 days)"; `today.json` and `/pkg/*/index.json` gain `prev_version` (T-0117). Version comparison only.
"No breaking keyword found in the release notes (checked <build date> UTC); notes can omit changes.", or
"Notes not fetched in this build" (T-0163; the home "Versions without release notes" section opens with a line of the three build.json counts) when `notes_kind != github_release`. Labels are a version comparison plus the regex only.
Card label (T-0111): when the repo's releases list was read (HTTP 200) and `raw.githubusercontent.com/<repo>/HEAD/CHANGELOG.md` (fetched once per repo per build) answered 200/404 without mentioning the version, the card says "No release notes found on GitHub Releases or in CHANGELOG.md for <version> (checked <date> UTC)" and the home page lists these in "Versions without release notes"; any fetch error keeps "Release notes not fetched". CHANGELOG text is never quoted.
Reuse (T-0164): when a version's notes cannot be fetched in this run (offline, `--no-github`, rate limit, API error) and the previous committed `/pkg/<eco>/<name>/index.json` holds that same version's notes result (GitHub release flag/quotes/link, or a dated checked-no-notes record), the record is reused, not marked not fetched; `build.json` `notes_reused` counts them. Never reused across versions.
Coverage (T-0115): the home "Versions without release notes" block shows three counts of versions from the same data as build.json: checked-no-notes, not checked because the notes were not fetched (`not_fetched`), not checked because no repo URL is known (`no_repo_url`); the last two are listed in a collapsed "Not checked" details. build.json gets a new key `notes_coverage.counts` (`checked_no_notes`, `not_fetched`, `no_repo_url`, in versions; `missing` groups >5 versions of a package into one entry with `versions`).
Home "Versions without release notes" (T-0112): a native closed `<details id="no-notes">` whose `<summary>` carries the label and N; with N=0 a plain heading with 0 and no `<details>`. No JavaScript.
T-0116: the "Versions without release notes" block splits N into S stable and P pre-release/dev (S+P=N; pre-release = `is_prerelease`, a semver hyphen suffix or a PEP 440 a/b/rc/dev marker), each in its own closed `<details id="no-notes-stable">` / `<details id="no-notes-pre">` with its count in the `<summary>`; the heading is now a plain `<h2 id="no-notes">`.
T-0165: the home heading reads "Versions checked with no release notes (N)", N = `notes_coverage.counts.checked_no_notes` of build.json; the three-count line stays under it.

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

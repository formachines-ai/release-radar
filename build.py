#!/usr/bin/env python3
"""Release Radar — static site generator (releases.formachines.ai).

Reads config/packages.txt, asks PyPI and the npm registry for the latest releases, pulls GitHub release notes
for releases of the last WINDOW_DAYS days, flags breaking-change wording with a regular expression (verbatim
quote, no model judgement), links packages that have a verified correction on formachines.ai/errata, and
writes the whole site into public/.

Standard library only (Python 3.12 on the server; 3.10+ works). No pip, no JavaScript on the site.

Usage:
    python3 build.py                 # full build (network)
    python3 build.py --offline       # rebuild from data/ caches only
    python3 build.py --no-github     # registries only, release notes = fallback links
    python3 build.py --limit 5       # first N packages (smoke test)
    GITHUB_TOKEN=... python3 build.py   # 5 000 GitHub requests/hour instead of 60
"""
from __future__ import annotations

import argparse
import gzip
import hashlib
import html
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

# ----------------------------------------------------------------------------------------------------------
# Parameters (everything tunable lives here)
# ----------------------------------------------------------------------------------------------------------
SITE_NAME = "Release Radar"
REPO_ISSUES_URL = "https://github.com/formachines-ai/release-radar/issues/new"
SITE_BASE = os.environ.get("SITE_BASE", "https://releases.formachines.ai").rstrip("/")
RESPONSIBLE = "Dmitry, formachines.ai"
RESPONSIBLE_URL = "https://formachines.ai/"
LEGAL_UPSTREAM = "https://formachines.ai/legal/"
ERRATA_INDEX_URL = "https://formachines.ai/errata/index.json"
ERRATA_SKILL_URL = "https://github.com/formachines-ai/skills"
LICENSE_URL = "https://creativecommons.org/licenses/by/4.0/"

ROOT = Path(__file__).resolve().parent
CONFIG_DIR = ROOT / "config"
DATA_DIR = ROOT / "data"
STATIC_DIR = ROOT / "static"
PUBLIC_DIR = ROOT / "public"
PACKAGES_FILE = CONFIG_DIR / "packages.txt"
IGNORE_FILE = CONFIG_DIR / "ignore_patterns.txt"
STATE_FILE = DATA_DIR / "state.json"

WINDOW_DAYS = 7            # releases shown on the front page and in the feed
RECENT_HOURS = 24          # "last 24 h" block
QUIET_HOURS = 48           # if nothing in 24 h, widen the recent block to this
HTTP_TIMEOUT = 15          # seconds per request
HTTP_RETRIES = 2
USER_AGENT = f"{SITE_NAME} ({SITE_BASE}; agent-team bot; responsible: owner@formachines.ai)"
GITHUB_API = "https://api.github.com"
GITHUB_RELEASES_PER_PAGE = 50
CHANGELOG_RAW_URL = "https://raw.githubusercontent.com/{repo}/HEAD/CHANGELOG.md"
GITHUB_MIN_REMAINING = 2   # stop calling GitHub when the remaining quota drops to this
NOTES_MAX_CHARS = 60_000   # release body is cached up to this many characters
QUOTE_MAX_CHARS = 280      # verbatim quote length cap (cut with an ellipsis, never rewritten)
QUOTE_SHORT_LINE = 48      # a matched line shorter than this gets the previous line prepended (1–2 lines)
MAX_QUOTES_PER_RELEASE = 3
NPM_TIME_KEEP_DAYS = 90    # npm `time` map is trimmed to this many days in the cache (full docs are huge)
PYPI_RELEASES_KEEP = 40    # newest PyPI versions kept in the cache

# The breaking-change pattern. Only verbatim matches of this count; see config/ignore_patterns.txt.
BREAKING_RE = re.compile(
    r"\b(?:break(?:ing|s)?|deprecat(?:e|es|ed|ion|ions|ing)|removed|renamed|drop(?:ped|s)?\s+support|no longer)\b",
    re.IGNORECASE,
)
PRERELEASE_RE = re.compile(r"(?:^|[.\-])(?:a|b|rc|alpha|beta|dev|pre|canary|next|nightly|insiders)\d*(?:[.\-]|$)", re.I)
PEP440_PRE_RE = re.compile(r"^v?\d+(?:\.\d+)*[-_.]?(?:a|b|c|rc|alpha|beta|pre|preview)[-_.]?\d*(?:$|[.+\-_])|\.?dev\d*", re.I)


def is_prerelease(eco: str, ver: str) -> bool:
    """npm: semver '-' prerelease part (build metadata after '+' ignored); PyPI: PEP 440 a/b/rc/dev."""
    if eco == "npm":
        return "-" in ver.split("+", 1)[0]
    return bool(PEP440_PRE_RE.search(ver)) or bool(PRERELEASE_RE.search(ver))

PLATFORM_VARIANT_RE = re.compile(r"^(?P<base>.+)-(?:darwin|linux|win32)-(?:x64|arm64)$")
GROUP_MISSING_OVER = 5  # notes_coverage.missing: group entries sharing eco+name+reason when there are more than this


def variant_base(eco: str, ver: str) -> str | None:
    """npm per-platform build (e.g. 0.167.0-linux-x64, confirmed in today.json for @openai/codex) -> base version."""
    if eco != "npm":
        return None
    m = PLATFORM_VARIANT_RE.match(ver)
    return m.group("base") if m else None


def group_variants(rels: list[dict]) -> list[tuple[dict, list[dict]]]:
    """Fold unflagged platform builds into the card of their base version when that card is in the same list."""
    present = {(r["ecosystem"], r["name"], r["version"]) for r in rels}
    groups: dict[tuple, list[dict]] = {}
    for r in rels:
        b = r.get("variant_of")
        if b and not r["flagged"] and (r["ecosystem"], r["name"], b) in present:
            groups.setdefault((r["ecosystem"], r["name"], b), []).append(r)
    out = []
    for r in rels:
        b = r.get("variant_of")
        if b and not r["flagged"] and (r["ecosystem"], r["name"], b) in present:
            continue
        out.append((r, groups.get((r["ecosystem"], r["name"], r["version"]), [])))
    return out


def render_cards(rels: list[dict], now: datetime, **kw) -> str:
    return "".join(render_card(r, now, variants=v, **kw) for r, v in group_variants(rels))


def notes_counts(missing: list[dict], checked_no_notes: int) -> dict:
    """Coverage counts (versions) from the same list that feeds build.json notes_coverage.missing."""
    return {"checked_no_notes": checked_no_notes,
            "not_fetched": sum(1 for m in missing if m["reason"] == "not_fetched"),
            "no_repo_url": sum(1 for m in missing if m["reason"] == "no_repo_url")}


def group_missing(missing: list[dict]) -> list[dict]:
    by: dict[tuple, list[dict]] = {}
    for m in missing:
        by.setdefault((m["eco"], m["name"], m["reason"]), []).append(m)
    out = []
    for (eco, name, reason), items in by.items():
        if len(items) > GROUP_MISSING_OVER:
            vs = sorted(i["version"] for i in items)
            out.append({"eco": eco, "name": name, "reason": reason, "count": len(vs), "versions": vs})
        else:
            out.extend(items)
    out.sort(key=lambda m: (m["eco"], m["name"].lower(), m.get("version") or m["versions"][0]))
    return out


SEMVER_NUM_RE = re.compile(r"^v?(\d+)(?:\.(\d+))?(?:\.(\d+))?$")


def version_tuple(ver: str) -> tuple[int, int, int] | None:
    """(major, minor, patch) for plain numeric versions like 1, 1.2, v1.2.3; None for anything else."""
    m = SEMVER_NUM_RE.match(ver.split("+", 1)[0].strip())
    return tuple(int(g or 0) for g in m.groups()) if m else None


def compute_bump(eco: str, ver: str, known: list[str]) -> str:
    """major|minor|patch|prerelease|unknown vs the highest known stable version below this one."""
    if is_prerelease(eco, ver):
        return "prerelease"
    cur = version_tuple(ver)
    if cur is None:
        return "unknown"
    prevs = [t for v in known if not is_prerelease(eco, v) and (t := version_tuple(v)) is not None and t < cur]
    if not prevs:
        return "unknown"
    prev = max(prevs)
    return "major" if cur[0] != prev[0] else "minor" if cur[1] != prev[1] else "patch"


log = logging.getLogger("build")


# ----------------------------------------------------------------------------------------------------------
# Small helpers
# ----------------------------------------------------------------------------------------------------------
def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_dt(s: str | None) -> datetime | None:
    if not s:
        return None
    s = s.strip()
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def esc(s: object) -> str:
    return html.escape(str(s if s is not None else ""), quote=True)


def read_json(path: Path) -> dict | list | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        log.debug("no cache at %s (%s)", path, e)
        return None


def write_json(path: Path, obj: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, ensure_ascii=False, indent=1, sort_keys=True) + "\n", encoding="utf-8")
    tmp.replace(path)


def write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def go_id(url: str) -> str:
    """Stable id for /go/<id>/ redirect pages: first 10 hex chars of sha1(url)."""
    return hashlib.sha1(url.encode("utf-8")).hexdigest()[:10]


def safe_fs_name(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9@._-]+", "_", name).replace("@", "")


GIT_SHA: str | None = None


def build_line(built: str) -> str:
    return f"build {esc(built)} UTC · {esc(GIT_SHA or 'unknown')}"


def git_head() -> str | None:
    try:
        out = subprocess.run(["git", "rev-parse", "HEAD"], cwd=ROOT, capture_output=True,
                             text=True, timeout=10, check=True).stdout.strip()
        return out[:7] if re.fullmatch(r"[0-9a-f]{40,64}", out) else None
    except (OSError, subprocess.SubprocessError):
        return None


# ----------------------------------------------------------------------------------------------------------
# HTTP
# ----------------------------------------------------------------------------------------------------------
class Http:
    def __init__(self, offline: bool = False):
        self.offline = offline
        self.github_remaining: int | None = None
        self.gh_problem: dict[str, str] = {}   # repo → "rate_limited" | "api_error" (last GitHub failure while reading it)
        self.github_reset: int | None = None
        self.gh_list_ok: set[str] = set()      # repos whose releases list was read (HTTP 200) in this build
        self.changelogs: dict[str, tuple[int, str]] = {}   # repo → (status, text) of CHANGELOG.md, once per build
        self.errors: list[str] = []
        self.requests = 0

    def get(self, url: str, headers: dict | None = None, accept_json: bool = True) -> tuple[int, bytes, dict]:
        """Returns (status, body, headers). Status 0 = transport error (already logged)."""
        if self.offline:
            return 0, b"", {}
        hdrs = {"User-Agent": USER_AGENT, "Accept-Encoding": "gzip"}
        if accept_json:
            hdrs["Accept"] = "application/json"
        if headers:
            hdrs.update(headers)
        last_err = ""
        for attempt in range(HTTP_RETRIES + 1):
            req = urllib.request.Request(url, headers=hdrs)
            self.requests += 1
            try:
                with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
                    body = resp.read()
                    if resp.headers.get("Content-Encoding", "").lower() == "gzip":
                        body = gzip.decompress(body)
                    return resp.status, body, dict(resp.headers)
            except urllib.error.HTTPError as e:
                body = b""
                try:
                    body = e.read()
                except OSError:
                    pass
                if e.code in (429, 503) and attempt < HTTP_RETRIES:
                    wait = 2 + attempt * 3
                    log.warning("HTTP %s for %s — retry in %ss", e.code, url, wait)
                    time.sleep(wait)
                    continue
                return e.code, body, dict(e.headers or {})
            except (urllib.error.URLError, TimeoutError, OSError, ValueError) as e:
                last_err = f"{type(e).__name__}: {e}"
                if attempt < HTTP_RETRIES:
                    time.sleep(1 + attempt)
                    continue
        msg = f"GET {url} failed: {last_err}"
        log.error(msg)
        self.errors.append(msg)
        return 0, b"", {}

    def get_json(self, url: str, headers: dict | None = None) -> tuple[int, object]:
        status, body, _ = self.get(url, headers)
        if status == 200 and body:
            try:
                return status, json.loads(body.decode("utf-8"))
            except ValueError as e:
                msg = f"bad JSON from {url}: {e}"
                log.error(msg)
                self.errors.append(msg)
                return 0, None
        return status, None

    def github(self, path: str) -> tuple[int, object]:
        if self.github_remaining is not None and self.github_remaining <= GITHUB_MIN_REMAINING:
            reset = datetime.fromtimestamp(self.github_reset or 0, tz=timezone.utc)
            log.warning("GitHub quota exhausted (resets %s) — skipping %s", iso(reset), path)
            return 429, None
        headers = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}
        token = os.environ.get("GITHUB_TOKEN", "").strip()
        if token:
            headers["Authorization"] = f"Bearer {token}"
        status, body, hdrs = self.get(GITHUB_API + path, headers)
        rem = hdrs.get("X-RateLimit-Remaining") or hdrs.get("x-ratelimit-remaining")
        rst = hdrs.get("X-RateLimit-Reset") or hdrs.get("x-ratelimit-reset")
        if rem is not None:
            try:
                self.github_remaining = int(rem)
                self.github_reset = int(rst) if rst else None
            except ValueError:
                pass
        if status == 200:
            try:
                return 200, json.loads(body.decode("utf-8"))
            except ValueError:
                return 0, None
        if status in (403, 429):
            log.warning("GitHub %s on %s (remaining=%s)", status, path, self.github_remaining)
        return status, None


# ----------------------------------------------------------------------------------------------------------
# Config
# ----------------------------------------------------------------------------------------------------------
def load_packages(limit: int | None = None) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for raw in PACKAGES_FILE.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if ":" not in line:
            log.warning("packages.txt: skipping malformed line %r", line)
            continue
        eco, name = line.split(":", 1)
        eco, name = eco.strip().lower(), name.strip()
        if eco not in ("pypi", "npm") or not name:
            log.warning("packages.txt: skipping %r (ecosystem must be pypi or npm)", line)
            continue
        key = (eco, name.lower())
        if key in seen:
            continue
        seen.add(key)
        out.append((eco, name))
    if limit:
        out = out[:limit]
    log.info("packages: %d (%d pypi, %d npm)", len(out), sum(e == "pypi" for e, _ in out), sum(e == "npm" for e, _ in out))
    return out


def load_ignore_patterns() -> list[re.Pattern]:
    pats: list[re.Pattern] = []
    if not IGNORE_FILE.exists():
        return pats
    for raw in IGNORE_FILE.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        try:
            pats.append(re.compile(line, re.IGNORECASE))
        except re.error as e:
            log.warning("ignore_patterns.txt: bad regex %r (%s)", line, e)
    log.info("ignore patterns: %d", len(pats))
    return pats


# ----------------------------------------------------------------------------------------------------------
# Registries
# ----------------------------------------------------------------------------------------------------------
def github_repo_from_url(url: str | None) -> str | None:
    """'https://github.com/owner/repo/...' / 'git+ssh://git@github.com/owner/repo.git' → 'owner/repo'."""
    if not url or not isinstance(url, str):
        return None
    m = re.search(r"github\.com[/:]([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+?)(?:\.git)?(?:[/#?].*)?$", url.strip())
    if not m:
        return None
    owner, repo = m.group(1), m.group(2)
    if owner.lower() in ("sponsors", "orgs", "features", "topics"):
        return None
    return f"{owner}/{repo}"


def fetch_pypi(http: Http, name: str) -> dict | None:
    """Trimmed PyPI metadata: newest versions with dates, repo and changelog links."""
    cache = DATA_DIR / "registry" / "pypi" / f"{safe_fs_name(name)}.json"
    status, doc = http.get_json(f"https://pypi.org/pypi/{urllib.parse.quote(name)}/json")
    if status != 200 or not isinstance(doc, dict):
        if status not in (0, 200):
            http.errors.append(f"pypi {name}: HTTP {status}")
        cached = read_json(cache)
        if cached:
            log.warning("pypi %s: using cache from %s", name, cached.get("fetched"))
        return cached if isinstance(cached, dict) else None
    info = doc.get("info") or {}
    urls = {str(k): str(v) for k, v in (info.get("project_urls") or {}).items() if v}
    repo = None
    changelog = None
    for k, v in urls.items():
        kl = k.lower()
        if changelog is None and any(w in kl for w in ("changelog", "release notes", "changes", "news", "history")):
            changelog = v
    for k, v in list(urls.items()) + [("home_page", str(info.get("home_page") or ""))]:
        r = github_repo_from_url(v)
        if r and (repo is None or any(w in k.lower() for w in ("source", "repository", "code", "github"))):
            repo = r
            if any(w in k.lower() for w in ("source", "repository", "code", "github")):
                break
    versions: list[dict] = []
    for ver, files in (doc.get("releases") or {}).items():
        if not files:
            continue
        times = [parse_dt(f.get("upload_time_iso_8601") or f.get("upload_time")) for f in files]
        times = [t for t in times if t]
        if not times:
            continue
        versions.append({"version": ver, "date": iso(min(times)), "yanked": all(bool(f.get("yanked")) for f in files)})
    versions.sort(key=lambda v: v["date"], reverse=True)
    out = {
        "ecosystem": "pypi", "name": info.get("name") or name, "fetched": iso(utcnow()),
        "summary": (info.get("summary") or "")[:300], "latest": info.get("version"),
        "page": f"https://pypi.org/project/{name}/", "repo": repo, "changelog": changelog,
        "homepage": info.get("home_page") or urls.get("Homepage") or "",
        "versions": versions[:PYPI_RELEASES_KEEP],
    }
    write_json(cache, out)
    return out


def fetch_npm(http: Http, name: str) -> dict | None:
    """Trimmed npm registry metadata (the full document is trimmed before caching — it can be tens of MB)."""
    cache = DATA_DIR / "registry" / "npm" / f"{safe_fs_name(name)}.json"
    url = "https://registry.npmjs.org/" + urllib.parse.quote(name, safe="@")
    status, doc = http.get_json(url)
    if status != 200 or not isinstance(doc, dict):
        if status not in (0, 200):
            http.errors.append(f"npm {name}: HTTP {status}")
        cached = read_json(cache)
        if cached:
            log.warning("npm %s: using cache from %s", name, cached.get("fetched"))
        return cached if isinstance(cached, dict) else None
    tags = doc.get("dist-tags") or {}
    latest = tags.get("latest")
    repo_field = doc.get("repository")
    repo_url = repo_field.get("url") if isinstance(repo_field, dict) else repo_field
    repo = github_repo_from_url(repo_url) or github_repo_from_url(doc.get("homepage")) or github_repo_from_url(
        (doc.get("bugs") or {}).get("url") if isinstance(doc.get("bugs"), dict) else None)
    cutoff = utcnow() - timedelta(days=NPM_TIME_KEEP_DAYS)
    versions: list[dict] = []
    for ver, when in (doc.get("time") or {}).items():
        if ver in ("created", "modified"):
            continue
        dt = parse_dt(when)
        if not dt or (dt < cutoff and ver != latest):
            continue
        meta = (doc.get("versions") or {}).get(ver) or {}
        versions.append({"version": ver, "date": iso(dt), "deprecated": bool(meta.get("deprecated"))})
    versions.sort(key=lambda v: v["date"], reverse=True)
    out = {
        "ecosystem": "npm", "name": doc.get("name") or name, "fetched": iso(utcnow()),
        "summary": (doc.get("description") or "")[:300], "latest": latest, "dist_tags": {k: v for k, v in tags.items()},
        "page": f"https://www.npmjs.com/package/{name}", "repo": repo, "changelog": None,
        "homepage": doc.get("homepage") or "", "versions": versions,
    }
    write_json(cache, out)
    return out


# ----------------------------------------------------------------------------------------------------------
# GitHub release notes
# ----------------------------------------------------------------------------------------------------------
def note_gh_failure(http: Http, repo: str, status: int) -> None:
    """Remember why GitHub could not be read for a repo (used for notes_coverage reasons)."""
    if status in (403, 429):
        http.gh_problem[repo] = "rate_limited"
    elif status != 404:
        http.gh_problem[repo] = "api_error"


def fetch_github_releases(http: Http, repo: str, no_github: bool) -> list[dict]:
    """Newest releases of a repo (trimmed: tag, name, url, date, body). Cached in data/github/."""
    cache = DATA_DIR / "github" / f"{repo.replace('/', '__')}.json"
    cached = read_json(cache)
    if no_github or http.offline:
        return cached.get("releases", []) if isinstance(cached, dict) else []
    status, doc = http.github(f"/repos/{repo}/releases?per_page={GITHUB_RELEASES_PER_PAGE}")
    if status != 200 or not isinstance(doc, list):
        note_gh_failure(http, repo, status)
        if isinstance(cached, dict):
            log.warning("github %s: HTTP %s — using cache from %s", repo, status, cached.get("fetched"))
            return cached.get("releases", [])
        if status not in (0, 200):
            http.errors.append(f"github {repo}: HTTP {status}")
        return []
    http.gh_list_ok.add(repo)
    rels = []
    for r in doc:
        if not isinstance(r, dict) or r.get("draft"):
            continue
        rels.append({
            "tag": r.get("tag_name") or "", "name": r.get("name") or "", "url": r.get("html_url") or "",
            "date": r.get("published_at") or r.get("created_at") or "", "prerelease": bool(r.get("prerelease")),
            "body": (r.get("body") or "")[:NOTES_MAX_CHARS],
        })
    write_json(cache, {"repo": repo, "fetched": iso(utcnow()), "releases": rels})
    return rels


def guess_tag(releases: list[dict], version: str, pkg_name: str) -> str | None:
    """Infer the repo's tag style from its existing release tags and build the tag for this version."""
    short = pkg_name.split("/")[-1]
    tags = [r.get("tag") or "" for r in releases]
    if not tags:
        return None
    if any("@" in t for t in tags):
        return f"{pkg_name}@{version}"
    if any("==" in t for t in tags):
        return f"{pkg_name}=={version}"
    if sum(t.startswith("v") for t in tags) * 2 >= len(tags):
        return f"v{version}"
    if any(t.startswith(short + "-") for t in tags):
        return f"{short}-{version}"
    return version


def candidate_tags(releases: list[dict], version: str, pkg_name: str) -> list[str]:
    """Exact tag spellings for this version: 1.2.3, v1.2.3, name@1.2.3, @scope/name@1.2.3, name-v1.2.3 …
    Each is looked up by exact tag name; nothing is matched by similarity. The repo's own style comes first."""
    short = pkg_name.split("/")[-1]
    out: list[str] = []
    first = guess_tag(releases, version, pkg_name)
    for t in (first, version, f"v{version}", f"{pkg_name}@{version}", f"{short}@{version}", f"{short}-v{version}",
              f"{short}-{version}", f"rust-v{version}", f"{short}@v{version}", f"{pkg_name}@v{version}",
              "rel_" + version.replace(".", "_")):  # sqlalchemy/sqlalchemy: rel_2_1_8
        if t and t not in out:
            out.append(t)
    return out


def fetch_release_by_tag(http: Http, repo: str, tag: str, no_github: bool) -> dict | None:
    """One GitHub release by tag (for monorepos whose newest-50 list does not reach the window). Cached, 404 too."""
    cache = DATA_DIR / "github" / f"{repo.replace('/', '__')}.json"
    doc = read_json(cache)
    doc = doc if isinstance(doc, dict) else {"repo": repo, "fetched": iso(utcnow()), "releases": []}
    by_tag: dict = doc.setdefault("by_tag", {})
    if tag in by_tag:
        return by_tag[tag]
    if no_github or http.offline:
        return None
    status, r = http.github(f"/repos/{repo}/releases/tags/{urllib.parse.quote(tag, safe='')}")
    rel = None
    if status == 200 and isinstance(r, dict) and not r.get("draft"):
        rel = {"tag": r.get("tag_name") or tag, "name": r.get("name") or "", "url": r.get("html_url") or "",
               "date": r.get("published_at") or r.get("created_at") or "", "prerelease": bool(r.get("prerelease")),
               "body": (r.get("body") or "")[:NOTES_MAX_CHARS]}
    if status not in (200, 404):
        note_gh_failure(http, repo, status)
    if status in (200, 404):
        by_tag[tag] = rel
        write_json(cache, doc)
    return rel


PEP440_NORM_RE = re.compile(r"[-_.]?(alpha|beta|preview|pre|rc|a|b|c|dev)[-_.]?(\d+)")


def pep440_norm(s: str) -> str:
    """PEP 440 spelling of a pre-release suffix: '1.107.0-rc.3' and '1.107.0rc3' are the same version."""
    s = PEP440_NORM_RE.sub(lambda m: {"alpha": "a", "beta": "b", "preview": "rc", "pre": "rc", "c": "rc"}.get(
        m.group(1), m.group(1)) + m.group(2), s.lower())
    return s


def match_release(releases: list[dict], version: str, pkg_name: str, eco: str = "") -> dict | None:
    """Find the GitHub release whose tag names this version (v1.2.3, 1.2.3, pkg@1.2.3, pkg-v1.2.3, pkg==1.2.3)."""
    v0 = version.lower()
    short = pkg_name.lower().split("/")[-1]
    for r in releases:
        v = v0
        tag = (r.get("tag") or "").lower()
        if eco == "pypi" and not tag.endswith(v) and pep440_norm(tag).endswith(pep440_norm(v)):
            tag, v = pep440_norm(tag), pep440_norm(version)
        if not tag.endswith(v):
            continue
        prefix = tag[: -len(v)]
        if prefix in ("", "v", "@", "=", "==", "-", "_", "/", "-v", "_v", "/v", "@v"):
            return r
        for sep in ("@", "@v", "-", "-v", "_", "_v", "/", "/v", "==", "=", "="):
            if prefix == short + sep or prefix.endswith("/" + short + sep) or prefix.endswith("-" + short + sep):
                return r
    return None


# ----------------------------------------------------------------------------------------------------------
# Breaking-change detection (regex + verbatim quote; nothing else)
# ----------------------------------------------------------------------------------------------------------
def find_quotes(body: str, ignore: list[re.Pattern]) -> list[dict]:
    """Verbatim 1–2 line quotes around each regex match. Returns [{quote, match, line_no}]."""
    if not body:
        return []
    lines = body.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    out: list[dict] = []
    seen_lines: set[int] = set()
    for i, line in enumerate(lines):
        if i in seen_lines:
            continue
        for m in BREAKING_RE.finditer(line):
            if any(_overlaps(m.span(), ig.span()) for ig in _all_matches(ignore, line)):
                continue
            text = line.strip()
            if len(text) < QUOTE_SHORT_LINE:
                if text.startswith("#"):
                    nxt = next((l.strip() for l in lines[i + 1:i + 4] if l.strip()), "")
                    text = text + ("\n" + nxt if nxt else "")
                elif i > 0 and lines[i - 1].strip():
                    text = lines[i - 1].strip() + "\n" + text
            if len(text) > QUOTE_MAX_CHARS:
                # keep the matched word inside the cut
                start = max(0, text.lower().find(m.group(0).lower()) - QUOTE_MAX_CHARS // 2)
                text = ("…" if start > 0 else "") + text[start:start + QUOTE_MAX_CHARS].rstrip() + "…"
            out.append({"quote": text, "match": m.group(0), "line_no": i + 1})
            seen_lines.add(i)
            break
        if len(out) >= MAX_QUOTES_PER_RELEASE:
            break
    return out


def _all_matches(patterns: list[re.Pattern], line: str):
    for p in patterns:
        yield from p.finditer(line)


def _overlaps(a: tuple[int, int], b: tuple[int, int]) -> bool:
    return a[0] < b[1] and b[0] < a[1]


# ----------------------------------------------------------------------------------------------------------
# Errata index
# ----------------------------------------------------------------------------------------------------------
def fetch_errata(http: Http) -> dict[tuple[str, str], dict]:
    cache = DATA_DIR / "errata" / "index.json"
    status, doc = http.get_json(ERRATA_INDEX_URL)
    if status == 200 and isinstance(doc, dict) and isinstance(doc.get("packages"), list):
        write_json(cache, doc)
    else:
        doc = read_json(cache)
        if doc:
            log.warning("errata index: HTTP %s — using cache from %s", status, doc.get("updated") if isinstance(doc, dict) else "?")
        else:
            log.warning("errata index unavailable and no cache")
            return {}
    out: dict[tuple[str, str], dict] = {}
    for p in doc.get("packages", []):
        if not isinstance(p, dict):
            continue
        key = (str(p.get("ecosystem", "")).lower(), str(p.get("package", "")).lower())
        out[key] = {
            "url": p.get("url"), "json": p.get("json"), "errata": p.get("errata"), "updated": p.get("updated"),
            "new_version": p.get("new_version"), "old_version": p.get("old_version"),
        }
    log.info("errata index: %d packages (%s)", len(out), doc.get("updated"))
    return out


# ----------------------------------------------------------------------------------------------------------
# Build model
# ----------------------------------------------------------------------------------------------------------
MISS_REASONS = ("no_repo_url", "no_matching_tag", "api_error", "rate_limited", "not_fetched")


def load_repo_overrides() -> dict[tuple[str, str], str]:
    """config/repo_overrides.txt: `<eco> <name> <owner/repo>` per line (optional file, # comments)."""
    path = ROOT / "config" / "repo_overrides.txt"
    out: dict[tuple[str, str], str] = {}
    if not path.exists():
        return out
    for line in path.read_text(encoding="utf-8").splitlines():
        parts = line.split("#", 1)[0].split()
        if len(parts) == 3 and parts[0] in ("pypi", "npm") and re.fullmatch(r"[\w.-]+/[\w.-]+", parts[2]):
            out[(parts[0], parts[1].lower())] = parts[2]
        elif parts:
            log.warning("repo_overrides.txt: bad line %r", line)
    return out


def fetch_changelog(http: Http, repo: str) -> tuple[int, str]:
    """CHANGELOG.md of a repo from raw.githubusercontent.com, once per build. Status 0 = fetch error.
    Only used to test whether a version string is mentioned; the text is never quoted or stored."""
    if repo not in http.changelogs:
        n_err = len(http.errors)
        status, body, _ = http.get(CHANGELOG_RAW_URL.format(repo=repo), accept_json=False)
        del http.errors[n_err:]   # a failed optional fetch is not a build error: the card just keeps "not fetched"
        text = body.decode("utf-8", "replace") if status == 200 else ""
        http.changelogs[repo] = (status, text)
    return http.changelogs[repo]


def changelog_mentions(text: str, version: str) -> bool:
    return re.search(r"(?<![0-9A-Za-z.])" + re.escape(version) + r"(?![0-9A-Za-z]|\.\d)", text, re.I) is not None


def no_notes_anywhere(http: Http, repo: str | None, ver: str) -> bool:
    """True only if the releases list was read, CHANGELOG.md answered 200/404 and neither mentions the version."""
    if not repo or repo not in http.gh_list_ok or repo in http.gh_problem or http.offline:
        return False
    status, text = fetch_changelog(http, repo)
    if status == 404:
        return True
    return status == 200 and not changelog_mentions(text, ver)


def miss_reason(http: Http, repo: str | None, gh_releases: list[dict], no_github: bool) -> str:
    """Why a release has no GitHub release notes. One of MISS_REASONS."""
    if not repo:
        return "no_repo_url"
    if repo in http.gh_problem:
        return http.gh_problem[repo]
    if no_github or http.offline:
        return "not_fetched"
    return "no_matching_tag"


def collect(http: Http, packages: list[tuple[str, str]], ignore: list[re.Pattern], errata: dict, no_github: bool) -> dict:
    now = utcnow()
    window_start = now - timedelta(days=WINDOW_DAYS)
    pkgs: list[dict] = []
    releases: list[dict] = []
    repo_cache: dict[str, list[dict]] = {}
    missing: list[dict] = []
    overrides = load_repo_overrides()
    for n, (eco, name) in enumerate(packages, 1):
        log.info("[%d/%d] %s:%s", n, len(packages), eco, name)
        meta = fetch_pypi(http, name) if eco == "pypi" else fetch_npm(http, name)
        if not meta:
            log.error("%s:%s — no metadata (skipped)", eco, name)
            http.errors.append(f"{eco} {name}: no metadata")
            continue
        err = errata.get((eco, name.lower()))
        if (eco, name.lower()) in overrides:
            meta["repo"] = overrides[(eco, name.lower())]
        pkg = {
            "ecosystem": eco, "name": name, "summary": meta.get("summary") or "", "latest": meta.get("latest"),
            "page": meta.get("page"), "repo": meta.get("repo"), "changelog": meta.get("changelog"),
            "homepage": meta.get("homepage") or "", "fetched": meta.get("fetched"),
            "errata": err, "releases": [], "path": f"/pkg/{eco}/{name}/",
        }
        stable = None
        vers_all = meta.get("versions", [])
        lat = meta.get("latest")
        if lat and not is_prerelease(eco, lat):
            stable = next((v for v in vers_all if v["version"] == lat), None) or {"version": lat, "date": None}
        else:
            stable = next((v for v in vers_all if not is_prerelease(eco, v["version"])), None)
        pkg["latest_stable"] = {"version": stable["version"], "date": stable.get("date")} if stable else None
        recent = [v for v in vers_all if (d := parse_dt(v.get("date"))) and d >= window_start]
        gh_releases: list[dict] = []
        if recent and pkg["repo"]:
            if pkg["repo"] not in repo_cache:
                repo_cache[pkg["repo"]] = fetch_github_releases(http, pkg["repo"], no_github)
            gh_releases = repo_cache[pkg["repo"]]
        for v in recent:
            ver = v["version"]
            gh = match_release(gh_releases, ver, name, eco)
            if gh is None and gh_releases and pkg["repo"] and not is_prerelease(eco, ver):
                for tag in candidate_tags(gh_releases, ver, name):
                    gh = fetch_release_by_tag(http, pkg["repo"], tag, no_github)
                    if gh:
                        break
            notes_url = None
            notes_kind = "fallback"
            quotes: list[dict] = []
            if not (gh and gh.get("url")):
                missing.append({"eco": eco, "name": name, "version": ver,
                                "reason": miss_reason(http, pkg["repo"], gh_releases, no_github)})
            no_notes = False
            if not (gh and gh.get("url")) and not no_github:
                no_notes = no_notes_anywhere(http, pkg["repo"], ver)
            if gh and gh.get("url"):
                notes_url = gh["url"]
                notes_kind = "github_release"
                quotes = find_quotes(gh.get("body") or "", ignore)
            elif pkg["changelog"]:
                notes_url, notes_kind = pkg["changelog"], "changelog"
            elif pkg["repo"]:
                notes_url, notes_kind = f"https://github.com/{pkg['repo']}/releases", "repo_releases"
            else:
                notes_url, notes_kind = pkg["page"], "registry_page"
            rel = {
                "id": f"{eco}/{name}/{ver}", "ecosystem": eco, "name": name, "version": ver, "date": v["date"],
                "variant_of": variant_base(eco, ver),
                "prerelease": is_prerelease(eco, ver) or bool(gh and gh.get("prerelease")),
                "yanked": bool(v.get("yanked")) or bool(v.get("deprecated")),
                "notes_url": notes_url, "notes_kind": notes_kind, "notes_go": go_id(notes_url) if notes_url else None,
                "bump": "prerelease" if gh and gh.get("prerelease") else compute_bump(eco, ver, [x["version"] for x in vers_all] + ([lat] if lat else [])),
                "flagged": bool(quotes), "quotes": quotes, "no_notes": no_notes, "pkg_path": pkg["path"],
                "errata_url": err["url"] if err else None, "errata_go": go_id(err["url"]) if err and err.get("url") else None,
            }
            pkg["releases"].append(rel)
            releases.append(rel)
        pkgs.append(pkg)
    releases.sort(key=lambda r: r["date"], reverse=True)
    pkgs.sort(key=lambda p: (p["ecosystem"], p["name"].lower()))
    missing.sort(key=lambda m: (m["eco"], m["name"].lower(), m["version"]))
    return {"built": iso(now), "window_start": iso(window_start), "packages": pkgs, "releases": releases,
            "notes_missing": missing}


# ----------------------------------------------------------------------------------------------------------
# HTML rendering
# ----------------------------------------------------------------------------------------------------------
def layout(title: str, body: str, built: str, description: str = "", canonical: str = "", noindex: bool = False,
           jsonld: dict | None = None, og_type: str = "website", extra_head: str = "", og_description: str = "") -> str:
    desc = description or "Daily feed of PyPI and npm releases with verbatim breaking-change quotes and links to verified Errata corrections."
    ogd = og_description or desc
    robots = '<meta name="robots" content="noindex">\n' if noindex else ""
    canon = f'<link rel="canonical" href="{esc(canonical)}">\n' if canonical else ""
    og = (f'<meta property="og:title" content="{esc(title)}">\n<meta property="og:description" content="{esc(ogd)}">\n'
          f'<meta name="twitter:card" content="summary">\n<meta name="twitter:title" content="{esc(title)}">\n'
          f'<meta name="twitter:description" content="{esc(ogd)}">\n'
          f'<meta property="og:type" content="{esc(og_type)}">\n'
          + (f'<meta property="og:url" content="{esc(canonical)}">\n' if canonical else "")
          + f'<meta property="og:site_name" content="{esc(SITE_NAME)}">\n')
    ld = ""
    if jsonld is None:
        jsonld = {"@type": "WebPage", "name": title, "description": desc, "inLanguage": "en"}
        if canonical:
            jsonld["url"] = canonical
    data = json.dumps({"@context": "https://schema.org", **jsonld}, ensure_ascii=True, indent=1).replace("</", "<\\/")
    ld = f'<script type="application/ld+json">\n{data}\n</script>\n'
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{esc(title)}</title>
<meta name="description" content="{esc(desc)}">
{og}{ld}<meta name="generator" content="Release Radar build.py (agent-team)">
{robots}{canon}<link rel="stylesheet" href="/style.css">
<link rel="icon" href="/brand/avatar.svg" type="image/svg+xml">
<link rel="alternate" type="application/rss+xml" title="{esc(SITE_NAME)} RSS" href="/feed.xml">
{extra_head}</head>
<body>
<header class="site"><div class="wrap">
<a class="brand" href="/">{esc(SITE_NAME)}</a>
<nav><a href="/#pkg-index">jump to package index</a><a href="/#pypi">PyPI</a><a href="/#npm">npm</a><a href="/#packages">Packages</a><a href="/feed.xml">RSS</a><a href="/today.json">today.json</a><a href="/llms.txt">llms.txt</a><a href="/legal/">Legal</a></nav>
</div></header>
<main><div class="wrap">
{body}
</div></main>
<footer class="site"><div class="wrap">
<p>Built by AI agents (agent-team) without human editing · responsible: <a href="{esc(RESPONSIBLE_URL)}">{esc(RESPONSIBLE)}</a> · updated {esc(built)} UTC</p>
<p class="build">{build_line(built)}</p>
<p>Data: public registries (PyPI, npm) and GitHub Releases. Breaking-change flags are regular-expression matches with a verbatim quote — not a judgement. <a href="{esc(LICENSE_URL)}">CC BY 4.0</a> · <a href="/legal/">Legal</a> · <a href="/llms.txt">llms.txt</a> · <a href="/mcp/">MCP</a> · no cookies, no tracking, no JavaScript.</p>
</div></footer>
</body>
</html>
"""


def fmt_date(s: str | None) -> str:
    dt = parse_dt(s)
    return dt.strftime("%Y-%m-%d %H:%M") + " UTC" if dt else "unknown date"


def fmt_ago(s: str | None, now: datetime) -> str:
    dt = parse_dt(s)
    if not dt:
        return ""
    h = (now - dt).total_seconds() / 3600
    if h < 1:
        return "just now"
    if h < 48:
        return f"{int(h)} h ago"
    return f"{int(h // 24)} d ago"


def render_quote(q: dict) -> str:
    text = esc(q["quote"])
    m = esc(q["match"])
    # bold the matched word once (case-insensitive) — only presentation, the text is untouched
    idx = text.lower().find(m.lower())
    if idx >= 0:
        text = text[:idx] + '<span class="m">' + text[idx:idx + len(m)] + "</span>" + text[idx + len(m):]
    return f'<blockquote class="quote">{text}</blockquote>'


def vote_id(r: dict) -> str:
    return hashlib.sha1(r["id"].encode("utf-8")).hexdigest()[:10]


def vote_target(r: dict) -> str:
    return f'{r["pkg_path"]}#{safe_fs_name(r["id"])}'


def render_card(r: dict, now: datetime, show_pkg_link: bool = True, notes_top: bool = False, variants: list | None = None) -> str:
    flagged = ' flagged' if r["flagged"] else ""
    badges = []
    if r["flagged"]:
        badges.append('<span class="badge flag">breaking / deprecated wording</span>')
    if r["flagged"] and r["bump"] in ("minor", "patch"):
        badges.append(f'<span class="badge flag bumpflag">Breaking keyword in a {r["bump"]} release</span>')
    if r.get("errata_url"):
        badges.append(f'<a class="badge errata" href="/go/{esc(r["errata_go"])}/" rel="nofollow">Errata: verified correction</a>')
    pre = '<span class="pre">pre-release</span>' if r["prerelease"] else ""
    bump = f'<span class="pre bump">{esc(r["bump"])}</span>'
    if r["flagged"]:
        honest = ""
    elif r.get("no_notes"):
        honest = (f'<p class="note nonote">No release notes found on GitHub Releases or in CHANGELOG.md for '
                  f'{esc(r["version"])} (checked {now.strftime("%Y-%m-%d")} UTC)</p>')
    elif r["notes_kind"] != "github_release":
        honest = '<p class="note nonote">Release notes not fetched.</p>'
    else:
        honest = (f'<p class="note nonote">No breaking keyword found in the release notes (checked {now.strftime("%Y-%m-%d")} UTC); '
                  f'notes can omit changes.</p>')
    yanked = '<span class="pre">yanked / deprecated</span>' if r.get("yanked") else ""
    name_html = f'<a href="{esc(r["pkg_path"])}">{esc(r["name"])}</a>' if show_pkg_link else esc(r["name"])
    quotes = "".join(render_quote(q) for q in r["quotes"])
    kind = {"github_release": "release notes", "changelog": "changelog", "repo_releases": "releases on GitHub",
            "registry_page": "registry page"}.get(r["notes_kind"], "release notes")
    kind_hint = f' <span class="note">({kind})</span>' if kind != "release notes" else ""
    links = f'<a href="/go/{esc(r["notes_go"])}/" rel="nofollow">release notes →</a>{kind_hint}' if r.get("notes_go") else ""
    top = ""
    if notes_top:
        top = f'<p class="notes-top">{links}</p>' if links else '<p class="notes-top note">release notes not fetched</p>'
        links = ""
    vhtml = ""
    if variants:
        vs = "".join(f'<li><code>{esc(v["version"])}</code></li>' for v in variants)
        vhtml = f'<details class="variants"><summary>{len(variants)} platform builds</summary><ul>{vs}</ul></details>'
    links += f'<a href="{esc(r["pkg_path"])}index.json">JSON</a>'
    vid = vote_id(r)
    issue_title = urllib.parse.quote(f"Error in entry: {r['ecosystem']}/{r['name']} {r['version']}")
    issue_body = urllib.parse.quote(f"Entry: {SITE_BASE}{r['pkg_path']}#{safe_fs_name(r['id'])}\n\nWhat is wrong (quote / number / link)?\n")
    feedback = (f'<span class="feedback">Was this useful? <a href="/go/vote/{vid}/up/" rel="nofollow">useful</a> · '
                f'<a href="/go/vote/{vid}/down/" rel="nofollow">not useful</a> · '
                f'<a href="{REPO_ISSUES_URL}?title={issue_title}&amp;body={issue_body.replace("&", "&amp;")}" rel="nofollow">Report an error</a></span>')
    return (f'<li class="card{flagged}" id="{esc(safe_fs_name(r["id"]))}">'
            f'<div class="head"><span class="badge eco">{esc(r["ecosystem"])}</span><span class="name">{name_html}</span>'
            f'<span class="ver">{esc(r["version"])}</span>{bump}{pre}{yanked}'
            f'<span class="when" title="{esc(fmt_date(r["date"]))}">{esc(fmt_date(r["date"]))} · {esc(fmt_ago(r["date"], now))}</span>'
            f'{" ".join(badges)}</div>{top}{quotes}{honest}{vhtml}<div class="links">{links}</div><div class="feedback-row">{feedback}</div></li>')


def render_index(model: dict, errata_count: int) -> str:
    now = parse_dt(model["built"]) or utcnow()
    rels = sorted(model["releases"], key=lambda r: r["prerelease"])  # stable first (stable sort keeps date order)
    recent_hours = RECENT_HOURS
    recent = [r for r in rels if (parse_dt(r["date"]) or now) >= now - timedelta(hours=RECENT_HOURS)]
    quiet_note = ""
    if not recent:
        recent_hours = QUIET_HOURS
        recent = [r for r in rels if (parse_dt(r["date"]) or now) >= now - timedelta(hours=QUIET_HOURS)]
        quiet_note = f' <span class="note">(quiet day: no release in the last {RECENT_HOURS} h, showing {QUIET_HOURS} h)</span>'
    flagged = [r for r in rels if r["flagged"]]
    with_errata = [r for r in rels if r.get("errata_url")]
    stats = (
        f'<ul class="stats">'
        f'<li><b>{len(recent)}</b><span>releases, last {recent_hours} h</span></li>'
        f'<li><b>{len(rels)}</b><span>releases, last {WINDOW_DAYS} days</span></li>'
        f'<li><b>{len(flagged)}</b><span>with breaking / deprecated wording</span></li>'
        f'<li><b>{len(with_errata)}</b><span>in packages with an Errata correction</span></li>'
        f'<li><b>{len(model["packages"])}</b><span>packages tracked</span></li>'
        f'</ul>'
    )
    nonotes = [r for r in rels if r.get("no_notes")]
    cov = notes_counts(model["notes_missing"], len(nonotes))
    by_ver = {(r["ecosystem"], r["name"], r["version"]): r for r in rels}
    unchecked = [m for m in model["notes_missing"] if m["reason"] in ("not_fetched", "no_repo_url")]
    nn_note = (f'<p class="note">No release notes found for {cov["checked_no_notes"]} of the versions checked; '
               f'{cov["not_fetched"]} versions could not be fetched and {cov["no_repo_url"]} have no known repository, '
               f'so they were not checked (build {now.strftime("%Y-%m-%d %H:%M")} UTC). '
               f'Checked = no matching GitHub release and no mention in the repository CHANGELOG.md, last {WINDOW_DAYS} days.</p>'
               f'<ul class="note nn-counts"><li>Checked, no notes found: <b>{cov["checked_no_notes"]}</b></li>'
               f'<li>Not checked, notes fetch failed (not_fetched): <b>{cov["not_fetched"]}</b></li>'
               f'<li>Not checked, no repository URL known (no_repo_url): <b>{cov["no_repo_url"]}</b></li></ul>')
    unchecked_html = ""
    if unchecked:
        items = []
        for m in unchecked:
            rr = by_ver.get((m["eco"], m["name"], m["version"]))
            path = rr["pkg_path"] if rr else f'/pkg/{m["eco"]}/{m["name"]}/'
            frag = f'#{esc(safe_fs_name(rr["id"]))}' if rr else ""
            items.append(f'<li><a href="{esc(path)}{frag}">{esc(m["name"])} {esc(m["version"])}</a> ({m["reason"]})</li>')
        unchecked_html = (f'<details id="not-checked" class="nonotes-block"><summary>Not checked: {len(unchecked)} versions</summary>'
                          f'<ul class="nonotes">{"".join(items)}</ul></details>')
    if nonotes:
        def _nn_pre(r):
            v = r["version"]
            return bool(r.get("prerelease")) or "-" in v.split("+", 1)[0] or bool(PEP440_PRE_RE.search(v)) or bool(PRERELEASE_RE.search(v))
        nn_pre = [r for r in nonotes if _nn_pre(r)]
        nn_st = [r for r in nonotes if not _nn_pre(r)]
        def _nn_li(rs):
            return "".join(f'<li><a href="{esc(r["pkg_path"])}#{esc(safe_fs_name(r["id"]))}">{esc(r["name"])} {esc(r["version"])}</a></li>' for r in rs)
        nonotes_block = (f'<h2 id="no-notes">Versions without release notes ({len(nonotes)})</h2>'
                         f'<p class="note nn-split">No release notes found: {len(nn_st)} stable versions, {len(nn_pre)} pre-release/dev builds (pre-releases often ship without notes)</p>'
                         f'{nn_note}'
                         f'<details id="no-notes-stable" class="nonotes-block"><summary>Stable versions without release notes ({len(nn_st)})</summary>'
                         f'<ul class="nonotes">{_nn_li(nn_st)}</ul></details>'
                         f'<details id="no-notes-pre" class="nonotes-block"><summary>Pre-release/dev builds without release notes ({len(nn_pre)})</summary>'
                         f'<ul class="nonotes">{_nn_li(nn_pre)}</ul></details>{unchecked_html}')
    else:
        nonotes_block = f'<h2 id="no-notes">Versions without release notes (0)</h2>' + nn_note + unchecked_html
    flagged_block = ['<h2 id="flagged">Flagged in the last 7 days</h2>']
    flagged_block.append('<ul class="cards">' + render_cards(flagged, now) + "</ul>" if flagged
                else '<p class="note">Nothing is flagged in the last 7 days: no release notes matched the breaking-change pattern. All releases are listed below.</p>')
    body = [f"<h1>{esc(SITE_NAME)} — {esc(now.strftime('%Y-%m-%d'))}</h1>",
            '<p class="lead">Updating a Python or JavaScript dependency? Find its recent releases and jump to the original notes. '
            'Highlighted excerpts are word matches in the release notes worth checking; they do not confirm your app will break, '
            'and no flag does not mean a safe update.</p>',
            '<p class="follow"><b>Follow:</b> <a href="/feed.xml">RSS (/feed.xml)</a> · <a href="/feed-breaking.xml">Breaking-only RSS (/feed-breaking.xml)</a> · <a href="/ci-check.txt">CI check (/ci-check.txt)</a> · <a href="https://t.me/relesae_radar" rel="nofollow">Telegram (https://t.me/relesae_radar)</a></p>',
            "\n".join(flagged_block),
            nonotes_block,
            stats,
            f'<p class="toc"><a href="#recent">Last {recent_hours} h</a><a href="#flagged">Flagged</a><a href="#pypi">PyPI, {WINDOW_DAYS} days</a><a href="#npm">npm, {WINDOW_DAYS} days</a><a href="#packages">All packages</a></p>']
    idx = [f'<details id="pkg-index"><summary>Find your package ({len(model["packages"])})</summary>']
    for eco, label in (("npm", "npm"), ("pypi", "PyPI")):
        names = sorted((p for p in model["packages"] if p["ecosystem"] == eco), key=lambda p: p["name"].lower())
        if names:
            idx.append(f'<p class="pkg-index"><b>{label} ({len(names)}):</b> ' + " · ".join(
                f'<a href="{esc(p["path"])}">{esc(p["name"])}</a>' for p in names) + "</p>")
    idx.append('</details>')
    body.append("\n".join(idx))
    body.append(f'<h2 id="recent">Last {recent_hours} h{quiet_note}</h2>')
    body.append('<p class="note legend">Flag = regex word match + verbatim quote.</p>')
    body.append('<ul class="cards">' + render_cards(recent, now) + "</ul>" if recent
                else f'<p class="note">No release in the last {QUIET_HOURS} h among tracked packages. The {WINDOW_DAYS}-day lists below are never empty on purpose.</p>')
    body.append(f'<p class="note">How flags work: the last {WINDOW_DAYS} days of releases are read for the exact lines of the release notes that say <em>breaking</em>, <em>deprecated</em>, <em>removed</em>, <em>renamed</em>, '
                f'<em>dropped support</em> or <em>no longer</em>. Quotes are verbatim; the flag is a regular-expression match, not an opinion. '
                f'Packages with a sandbox-verified fix on <a href="https://formachines.ai/errata/">Errata</a> ({errata_count} packages there) carry a green badge.</p>')
    for eco, label in (("pypi", "PyPI"), ("npm", "npm")):
        sub = [r for r in rels if r["ecosystem"] == eco]
        body.append(f'<h2 id="{eco}">{esc(label)} — last {WINDOW_DAYS} days ({len(sub)})</h2>')
        body.append('<ul class="cards">' + render_cards(sub, now) + "</ul>" if sub
                    else f'<p class="note">No {label} release in the window.</p>')
    # package table
    rows = []
    for p in model["packages"]:
        e = p.get("errata")
        ebadge = f'<a class="badge errata" href="/go/{esc(go_id(e["url"]))}/" rel="nofollow">Errata ×{esc(e.get("errata"))}</a>' if e and e.get("url") else ""
        latest_date = p["releases"][0]["date"] if p["releases"] else None
        rows.append(f'<tr><td>{esc(p["ecosystem"])}</td><td><a href="{esc(p["path"])}">{esc(p["name"])}</a></td>'
                    f'<td><code>{esc(p.get("latest") or "?")}</code></td><td>{len(p["releases"])}</td>'
                    f'<td>{esc(fmt_date(latest_date)) if latest_date else "—"}</td><td>{ebadge}</td></tr>')
    body.append(f'<h2 id="packages">All tracked packages ({len(model["packages"])})</h2>')
    body.append('<div class="table-wrap"><table class="table"><thead><tr><th>eco</th><th>package</th><th>latest</th>'
                f'<th>releases / {WINDOW_DAYS} d</th><th>newest release</th><th>Errata</th></tr></thead><tbody>' + "".join(rows) + "</tbody></table></div>")
    body.append('<h2>For machines</h2><p class="note"><a href="/today.json">/today.json</a> — this page as data · '
                '<a href="/feed.xml">/feed.xml</a> — RSS · <a href="/feed-breaking.xml">/feed-breaking.xml</a> — RSS, flagged only · <code>/pkg/&lt;eco&gt;/&lt;name&gt;/index.json</code> — per package · '
                '<a href="/llms.txt">/llms.txt</a> · <a href="/build.json">/build.json</a>. Want a package added? It is one line in '
                '<code>config/packages.txt</code> of the public repository.</p>')
    title = f"{SITE_NAME} — PyPI and npm releases with breaking-change quotes"
    desc = ("Find recent PyPI and npm releases and open their original notes. "
            "Flags mark wording matches, not confirmed breaking changes.")
    ld = {"@type": ["WebSite", "Dataset"], "name": SITE_NAME, "description": desc, "url": SITE_BASE + "/",
          "dateModified": model["built"], "inLanguage": "en", "license": LICENSE_URL,
          "isAccessibleForFree": True,
          "distribution": [
              {"@type": "DataDownload", "encodingFormat": "application/json", "contentUrl": SITE_BASE + "/today.json"},
              {"@type": "DataDownload", "encodingFormat": "application/rss+xml", "contentUrl": SITE_BASE + "/feed.xml"}]}
    return layout(title, "\n".join(body), model["built"], description=desc, canonical=SITE_BASE + "/", jsonld=ld,
                  extra_head=f'<link rel="alternate" type="application/rss+xml" title="{esc(SITE_NAME)} — flagged releases only" href="/feed-breaking.xml">\n')


def related_packages(p: dict, model: dict, n: int = 4) -> list[dict]:
    """Deterministic: the n alphabetical neighbours (wrapping) of the same ecosystem."""
    same = sorted((x for x in model["packages"] if x["ecosystem"] == p["ecosystem"]), key=lambda x: x["name"].lower())
    names = [x["name"] for x in same]
    if p["name"] not in names or len(same) < 2:
        return []
    i = names.index(p["name"])
    n = min(n, len(same) - 1)
    out, step = [], 1
    while len(out) < n:
        for j in (i - step, i + step):  # previous then next neighbour
            if len(out) < n and 0 <= j < len(same):
                out.append(same[j])
        step += 1
        if step > len(same):
            break
    return sorted(out, key=lambda x: x["name"].lower())


def package_seo(p: dict) -> tuple[str, str]:
    """<title> and meta description of a package page: data and a verbatim quote fragment only."""
    name, eco, lat = p["name"], p["ecosystem"], p.get("latest")
    nv = f"{name} {lat}" if lat else name
    forms = [f"{nv} release notes & breaking changes ({eco}) — {SITE_NAME}",
             f"{nv} release notes & breaking changes ({eco})",
             f"{nv} release notes & breaking changes",
             f"{nv} breaking changes"]
    title = next((t for t in forms if len(t) <= 70), forms[-1])  # T-0096: first form that fits 70 chars
    rel = next((r for r in p["releases"] if r["version"] == lat), None)
    date = rel["date"] if rel else (p["latest_stable"].get("date") if p.get("latest_stable") and p["latest_stable"]["version"] == lat else None)
    nflag = sum(1 for r in p["releases"] if r["flagged"])
    head = f"{name} {lat or '?'}" + (f", released {fmt_date(date)}" if date else "") + \
        f"; {nflag} flagged release{'' if nflag == 1 else 's'} in the last {WINDOW_DAYS} days."
    fr = next((r for r in ([rel] if rel and rel["flagged"] else []) + [r for r in p["releases"] if r["flagged"]]), None)
    desc = head
    if fr:
        q = " ".join(fr["quotes"][0]["quote"].split())
        room = 160 - len(head) - len(' Quote: "…"') - 1
        if room >= 20:
            frag = q if len(q) <= room else q[:room].rsplit(" ", 1)[0].rstrip(" ,;:.") + "…"
            desc = f'{head} Quote: "{frag}"'
    return title, desc


def render_package(p: dict, model: dict) -> str:
    now = parse_dt(model["built"]) or utcnow()
    e = p.get("errata")
    meta = [("Ecosystem", esc(p["ecosystem"])), ("Latest version", f'<code>{esc(p.get("latest") or "?")}</code>'),
            ("Registry", f'<a href="{esc(p["page"])}" rel="nofollow">{esc(p["page"])}</a>')]
    if p.get("repo"):
        meta.append(("Repository", f'<a href="https://github.com/{esc(p["repo"])}" rel="nofollow">github.com/{esc(p["repo"])}</a>'))
    if p.get("changelog"):
        meta.append(("Changelog", f'<a href="{esc(p["changelog"])}" rel="nofollow">{esc(p["changelog"])}</a>'))
    meta.append(("Metadata fetched", esc(fmt_date(p.get("fetched")))))
    meta.append(("Data", f'<a href="{esc(p["path"])}index.json">index.json</a>'))
    meta.append(("Follow this package", f'<a href="{esc(p["path"])}feed.xml">RSS</a>'))
    body = [f'<h1>{esc(p["name"])} <span class="badge eco">{esc(p["ecosystem"])}</span></h1>']
    if p.get("summary"):
        body.append(f'<p class="lead">{esc(p["summary"])}</p>')
    body.append('<dl class="meta">' + "".join(f"<dt>{k}</dt><dd>{v}</dd>" for k, v in meta) + "</dl>")
    if e and e.get("url"):
        body.append(f'<h2>Errata</h2><p><a class="badge errata" href="/go/{esc(go_id(e["url"]))}/" rel="nofollow">Errata: {esc(e.get("errata"))} verified correction(s)</a> '
                    f'— checked on <code>{esc(e.get("old_version"))}</code> → <code>{esc(e.get("new_version"))}</code>, Errata entry updated {esc(e.get("updated"))}. '
                    f'The correction pages say on which versions the outdated code still reproduces; this site only links them.</p>')
    ls = p.get("latest_stable")
    if ls:
        ls_date = fmt_date(ls["date"]) if ls.get("date") else "date unknown"
        body.append(f'<p class="latest-stable"><b>Latest stable:</b> <code>{esc(ls["version"])}</code> ({esc(ls_date)}, source: registry)</p>')
    else:
        body.append('<p class="latest-stable"><b>Latest stable:</b> unknown (source: registry)</p>')
    n_pre = sum(1 for r in p["releases"] if r["prerelease"])
    n_stable = len(p["releases"]) - n_pre
    body.append(f'<h2>Releases in the last {WINDOW_DAYS} days ({len(p["releases"])}: {n_stable} stable, {n_pre} pre-releases)</h2>')
    if p["releases"] and n_stable == 0:
        if ls:
            ls_d = fmt_date(ls["date"]) if ls.get("date") else "date unknown"
            body.append(f'<p class="note no-stable">No stable release in the last {WINDOW_DAYS} days; latest stable: <code>{esc(ls["version"])}</code> ({esc(ls_d)}, source: registry)</p>')
        else:
            body.append(f'<p class="note no-stable">No stable release in the last {WINDOW_DAYS} days; latest stable: unknown (source: registry)</p>')
    if p["releases"]:
        ordered = sorted(p["releases"], key=lambda r: r["prerelease"])
        body.append('<ul class="cards">' + render_cards(ordered, now, show_pkg_link=False, notes_top=True) + "</ul>")
    else:
        body.append(f'<p class="note">No release in the last {WINDOW_DAYS} days. Latest known version: <code>{esc(p.get("latest") or "?")}</code>.</p>')
    sib = related_packages(p, model)
    if sib:
        body.append(f'<h2>Other {esc(p["ecosystem"])} packages tracked</h2><p class="pkg-index">' + " · ".join(
            f'<a href="{esc(x["path"])}">{esc(x["name"])}</a>' for x in sib) + "</p>")
    body.append('<p class="note"><a href="/">← Release Radar home: all tracked packages</a></p>')
    title, desc = package_seo(p)
    og_desc = (f"{p['name']} ({p['ecosystem']}): recent releases and links to their original notes. "
               "Flags mark wording matches, not confirmed breaking changes.")
    ld = {"@type": ["WebPage", "Dataset"], "name": f"{p['name']} ({p['ecosystem']})", "description": desc,
          "url": SITE_BASE + p["path"], "dateModified": model["built"], "inLanguage": "en",
          "license": LICENSE_URL, "keywords": [p["name"], p["ecosystem"]],
          "isPartOf": {"@type": "WebSite", "name": SITE_NAME, "url": SITE_BASE + "/"},
          "distribution": [{"@type": "DataDownload", "encodingFormat": "application/json",
                            "contentUrl": SITE_BASE + p["path"] + "index.json"}]}
    return layout(title, "\n".join(body), model["built"],
                  description=desc, og_description=og_desc, canonical=SITE_BASE + p["path"], jsonld=ld,
                  extra_head=f'<link rel="alternate" type="application/rss+xml" title="{esc(p["name"])} releases" href="{esc(p["path"])}feed.xml">\n')


def render_go(url: str, built: str, vote: bool = False) -> str:
    u = esc(url)
    msg = (f'Thanks — your vote is counted from the server log. <a href="{u}">Back to the entry</a>.' if vote else
           f'Redirecting to <a href="{u}" rel="nofollow">{u}</a>. This hop exists only so that external clicks can be counted in the server log; nothing about you is stored.')
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex, nofollow">
<meta http-equiv="refresh" content="0;url={u}">
<title>Redirecting — {esc(SITE_NAME)}</title>
<link rel="stylesheet" href="/style.css">
</head>
<body><main><div class="wrap">
<p>{msg}</p>
</div></main>
<footer class="site"><div class="wrap"><p>Built by AI agents (agent-team) without human editing · responsible: <a href="{esc(RESPONSIBLE_URL)}">{esc(RESPONSIBLE)}</a> · updated {esc(built)} UTC</p><p class="build">{build_line(built)}</p></div></footer>
</body>
</html>
"""


def render_404(built: str) -> str:
    body = (f"<h1>404 — no such page</h1><p class=\"lead\">Package pages live at <code>/pkg/pypi/&lt;name&gt;/</code> and "
            f"<code>/pkg/npm/&lt;name&gt;/</code> (scoped npm packages keep their scope: <code>/pkg/npm/@scope/name/</code>). "
            f"If the package you wanted is not tracked yet, this 404 is how we notice demand.</p>"
            f'<p><a href="/">Front page</a> · <a href="/#packages">All packages</a> · <a href="/feed.xml">RSS</a> · '
            f'<a href="/today.json">today.json</a> · <a href="/llms.txt">llms.txt</a> · <a href="/legal/">Legal</a></p>')
    return layout(f"404 — {SITE_NAME}", body, built, noindex=True)


def mcp_tools() -> list[tuple[str, str, list[str], list[str]]]:
    """Tool registry of mcp/release_radar_mcp.py (the TOOLS dict), read with ast: (name, description, args, required)."""
    import ast
    tree = ast.parse((Path(__file__).parent / "mcp" / "release_radar_mcp.py").read_text(encoding="utf-8"))
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(getattr(t, "id", "") == "TOOLS" for t in node.targets):
            out = []
            for k, v in zip(node.value.keys, node.value.values):
                desc = v.elts[1].value
                props = [p.value for p in v.elts[2].keys] if isinstance(v.elts[2], ast.Dict) else []
                req = [e.value for e in v.elts[3].elts]
                out.append((k.value, desc, props, req))
            return out
    raise RuntimeError("TOOLS registry not found in mcp/release_radar_mcp.py")


def render_mcp(built: str) -> str:
    src = f"{SITE_BASE}/mcp/release_radar_mcp.py"
    cc = f"claude mcp add release-radar -- python3 release_radar_mcp.py"
    cur = '{"mcpServers": {"release-radar": {"command": "python3", "args": ["/path/to/release_radar_mcp.py"]}}}'
    tools_html = "".join(
        f"<li><code>{esc(n)}({esc(', '.join(a if a in req else a + '?' for a in args))})</code> — {esc(d)}</li>"
        for n, d, args, req in mcp_tools())
    body = (f"<h1>MCP server for coding agents</h1>"
            f"<p class=\"lead\">Ask your coding agent what was flagged in a package before it writes code against a new major. "
            f"A stdio MCP server in one Python file (standard library only) that reads only our public JSON: "
            f"<a href=\"/today.json\">/today.json</a> and <code>/pkg/&lt;eco&gt;/&lt;name&gt;/index.json</code>. "
            f"It returns the fields as they are in the JSON — version, date, flag, verbatim quotes, notes link, Errata link — with no model-written text. A flag is a regex match, not a judgement.</p>"
            f"<h2>Tools</h2><ul>{tools_html}</ul>"
            f"<h2>Source</h2><p><a href=\"{src}\">{src}</a> (<a href=\"/mcp/release_radar_mcp.py.txt\">readable text copy</a>, "
            f"<a href=\"/mcp/README.md\">README</a>). Download it and save it anywhere; nothing is installed.</p>"
            f"<h2>Claude Code</h2><pre><code>curl -sO {src}\n{esc(cc)}</code></pre>"
            f"<h2>Cursor (<code>.cursor/mcp.json</code>)</h2><pre><code>{esc(cur)}</code></pre>"
            f"<p>Only GET requests to this site are made. Not published to npm or any registry.</p>")
    return layout(f"MCP server: what was flagged in package X — {SITE_NAME}", body, built,
                  description="Stdlib MCP server for coding agents over releases.formachines.ai JSON: flagged releases with verbatim quotes.",
                  canonical=f"{SITE_BASE}/mcp/")


def render_brand(built: str) -> str:
    def img(src: str, px: int) -> str:
        return f'<img src="{src}" width="{px}" height="{px}" alt="" style="border-radius:50%;vertical-align:middle;margin-right:.6rem">'
    body = (
        "<h1>Brand: avatar</h1>"
        "<p>Two avatar concepts: plain geometry, no text, no third-party logos, readable in a 48 px circle. "
        "The AI-agents note is not part of the picture; it is in every page footer and channel description.</p>"
        f'<h2>Chosen: A — radar sweep</h2><p>{img("/brand/avatar.svg", 192)}{img("/brand/avatar.svg", 48)}{img("/brand/avatar.svg", 24)}</p>'
        "<p>Why: it says “radar” at once, the high-contrast sweep and the orange blip survive downscaling to 48 px, "
        "and the blip reads as “something new appeared”. File: <a href=\"/brand/avatar.svg\">/brand/avatar.svg</a> (SVG 512×512). "
        "It is also the site favicon.</p>"
        f'<h2>Variant B — version stack</h2><p>{img("/brand/avatar-b.svg", 192)}{img("/brand/avatar-b.svg", 48)}</p>'
        "<p>Two stacked bars under an upward arrow (a version bump). Clear too, but generic: it looks like many upload/update icons. "
        "File: <a href=\"/brand/avatar-b.svg\">/brand/avatar-b.svg</a>.</p>"
        "<h2>Where it will be used</h2><ul>"
        "<li>Telegram channel @relesae_radar (to be uploaded by the owner or the duty session)</li>"
        "<li>Site favicon (done)</li></ul>"
        "<p>Other channels from the product plan (Bluesky, Mastodon, X) may reuse it once those accounts need an avatar.</p>")
    return layout(f"Brand — {SITE_NAME}", body, built, canonical=SITE_BASE + "/brand/",
                  description=f"Avatar concepts and the chosen avatar of {SITE_NAME}.")


def render_legal(built: str) -> str:
    body = (f"<h1>Legal</h1>"
            f"<p><strong>Responsible person:</strong> Dmitry, <a href=\"{esc(RESPONSIBLE_URL)}\">formachines.ai</a> — contact details, imprint and "
            f"privacy terms are on <a href=\"{esc(LEGAL_UPSTREAM)}\">{esc(LEGAL_UPSTREAM)}</a>, which applies to this site.</p>"
            f"<p><strong>Made by AI agents.</strong> Every page of {esc(SITE_NAME)} is generated and published by an autonomous team of AI agents "
            f"(agent-team) without human editing. The responsible person above answers for it.</p>"
            f"<p><strong>Data.</strong> Release dates and versions come from the public registries PyPI and npm; release notes from the GitHub "
            f"Releases API of the package's own repository. Quotes are reproduced verbatim and attributed by link; the breaking-change flag is a "
            f"regular-expression match, not an assessment, and may be wrong in both directions. Errata badges link to "
            f"<a href=\"https://formachines.ai/errata/\">formachines.ai/errata</a>.</p>"
            f"<p><strong>License.</strong> Our own text and data files (today.json, feed.xml, per-package JSON) are "
            f"<a href=\"{esc(LICENSE_URL)}\">CC BY 4.0</a>. Quoted release notes remain under their authors' licenses.</p>"
            f"<p><strong>No tracking.</strong> No cookies, no JavaScript, no forms, no analytics scripts. The web server keeps standard access logs "
            f"(IP address, user agent, requested path) for a limited time to count visits and to measure clicks on <code>/go/</code> redirect pages; "
            f"these logs are not shared.</p>"
            f"<p><strong>Removal.</strong> If you maintain a package and do not want it listed or quoted, write to the address on the page above; "
            f"one line in the public configuration removes it.</p>")
    return layout(f"Legal — {SITE_NAME}", body, built, canonical=SITE_BASE + "/legal/",
                  description=f"Responsible person, data sources, license and no-tracking statement of {SITE_NAME}. Built {built[:10]}.")


# ----------------------------------------------------------------------------------------------------------
# Machine-readable outputs
# ----------------------------------------------------------------------------------------------------------
def public_release(r: dict) -> dict:
    return {
        "id": r["id"], "ecosystem": r["ecosystem"], "package": r["name"], "version": r["version"], "date": r["date"],
        "prerelease": r["prerelease"], "variant_of": r.get("variant_of"), "yanked_or_deprecated": r["yanked"], "flagged": r["flagged"], "bump": r["bump"],
        "quotes": [{"quote": q["quote"], "match": q["match"]} for q in r["quotes"]],
        "notes_url": r["notes_url"], "notes_kind": r["notes_kind"], "notes_redirect": f"{SITE_BASE}/go/{r['notes_go']}/" if r["notes_go"] else None,
        "errata_url": r["errata_url"], "page": f"{SITE_BASE}{r['pkg_path']}", "json": f"{SITE_BASE}{r['pkg_path']}index.json",
    }


def render_feed(model: dict, releases: list | None = None, pkg: dict | None = None) -> str:
    items = []
    for r in (model["releases"] if releases is None else releases):
        title = f"{r['name']} {r['version']} ({r['ecosystem']})" + (" — breaking/deprecated wording" if r["flagged"] else "")
        desc_parts = []
        for q in r["quotes"]:
            desc_parts.append("Quote: " + q["quote"])
        desc_parts.append(f"Release notes: {r['notes_url']}")
        if r.get("errata_url"):
            desc_parts.append(f"Errata correction: {r['errata_url']}")
        dt = parse_dt(r["date"]) or utcnow()
        items.append(
            "<item>"
            f"<title>{esc(title)}</title>"
            f"<link>{esc(SITE_BASE + r['pkg_path'])}</link>"
            f"<guid isPermaLink=\"false\">{esc(r['id'])}</guid>"
            f"<pubDate>{dt.strftime('%a, %d %b %Y %H:%M:%S +0000')}</pubDate>"
            f"<category>{esc(r['ecosystem'])}</category>"
            f"<description>{esc(chr(10).join(desc_parts))}</description>"
            "</item>"
        )
    built = parse_dt(model["built"]) or utcnow()
    if pkg:
        ctitle = f"{SITE_NAME} — {pkg['name']} ({pkg['ecosystem']})"
        clink = SITE_BASE + pkg["path"]
        self_url = clink + "feed.xml"
        cdesc = f"Releases of {pkg['name']} ({pkg['ecosystem']}) in the last {WINDOW_DAYS} days; flagged ones carry a verbatim quote. "
    else:
        ctitle, clink, self_url = SITE_NAME, SITE_BASE + "/", SITE_BASE + "/feed.xml"
        cdesc = f"PyPI and npm releases of the last {WINDOW_DAYS} days with verbatim breaking-change quotes. "
    return ('<?xml version="1.0" encoding="UTF-8"?>\n<rss version="2.0" xmlns:atom="http://www.w3.org/2005/Atom"><channel>'
            f"<title>{esc(ctitle)}</title><link>{esc(clink)}</link>"
            f"<description>{esc(cdesc)}Built by AI agents (agent-team); responsible: {esc(RESPONSIBLE)}.</description>"
            f"<language>en</language><lastBuildDate>{built.strftime('%a, %d %b %Y %H:%M:%S +0000')}</lastBuildDate>"
            f'<atom:link href="{esc(self_url)}" rel="self" type="application/rss+xml"/>'
            + "".join(items) + "</channel></rss>\n")


def render_breaking_feed(model: dict) -> str:
    """RSS 2.0 with only the flagged releases of the window: verbatim quotes (max 3) and the /go/ notes link."""
    items = []
    for r in model["releases"]:
        if not r["flagged"]:
            continue
        link = f"{SITE_BASE}/go/{r['notes_go']}/" if r.get("notes_go") else SITE_BASE + r["pkg_path"]
        desc = [f"Quote: {q['quote']}" for q in r["quotes"][:3]]
        desc.append(f"Release notes: {link}")
        dt = parse_dt(r["date"]) or utcnow()
        items.append(
            "<item>"
            f"<title>{esc(r['name'] + '@' + r['version'])} ({esc(r['ecosystem'])}) — {dt.strftime('%Y-%m-%d')}</title>"
            f"<link>{esc(link)}</link>"
            f"<guid isPermaLink=\"false\">{esc(r['id'])}</guid>"
            f"<pubDate>{dt.strftime('%a, %d %b %Y %H:%M:%S +0000')}</pubDate>"
            f"<category>{esc(r['ecosystem'])}</category>"
            f"<description>{esc(chr(10).join(desc))}</description>"
            "</item>")
    built = parse_dt(model["built"]) or utcnow()
    cdesc = (f"Only the releases of the last {WINDOW_DAYS} days that are flagged. A flag is a regular-expression word match "
             "(breaking, deprecated, removed, renamed, dropped support, no longer) in the release notes plus a verbatim quote; "
             "no flag does not mean a release is safe. ")
    return ('<?xml version="1.0" encoding="UTF-8"?>\n<rss version="2.0" xmlns:atom="http://www.w3.org/2005/Atom"><channel>'
            f"<title>{esc(SITE_NAME)} — flagged releases only</title><link>{esc(SITE_BASE + '/')}</link>"
            f"<description>{esc(cdesc)}Built by AI agents (agent-team); responsible: {esc(RESPONSIBLE)}.</description>"
            f"<language>en</language><lastBuildDate>{built.strftime('%a, %d %b %Y %H:%M:%S +0000')}</lastBuildDate>"
            f'<atom:link href="{esc(SITE_BASE + "/feed-breaking.xml")}" rel="self" type="application/rss+xml"/>'
            + "".join(items) + "</channel></rss>\n")


def render_llms(model: dict) -> str:
    lines = [f"# {SITE_NAME}", "",
             f"> Daily feed of PyPI and npm releases ({len(model['packages'])} tracked packages, last {WINDOW_DAYS} days) with the verbatim lines of release notes that mention breaking, deprecated, removed, renamed, dropped support or no longer. Flags are regular-expression matches with a quote, never a model's judgement. Packages with a sandbox-verified fix on formachines.ai/errata carry a link.",
             "",
             "Built by AI agents (agent-team) without human editing; responsible: Dmitry, formachines.ai. License of our data: CC BY 4.0. No tracking.", "",
             f"Updated: {model['built']} UTC (daily build 06:00 UTC)", "",
             "## Data", "",
             f"- [today.json]({SITE_BASE}/today.json): every release in the window — package, version, date, flagged, quotes, notes_url, errata_url; top-level counts object (releases, packages, flagged, prereleases, with_errata) matching build.json",
             f"- [feed.xml]({SITE_BASE}/feed.xml): RSS 2.0 of the same releases",
             f"- [feed-breaking.xml]({SITE_BASE}/feed-breaking.xml): RSS 2.0 with only the flagged releases (regex match + verbatim quotes + notes link); valid empty channel if none",
             f"- [build.json]({SITE_BASE}/build.json): build time, package and release counts, errors",
             f"- Per package: {SITE_BASE}/pkg/<ecosystem>/<name>/index.json (ecosystem = pypi | npm; scoped npm names keep the @scope/)",
             f"- Per package RSS: {SITE_BASE}/pkg/<ecosystem>/<name>/feed.xml (RSS 2.0, that package's releases in the window; empty channel if none)",
             "", "## Pages", "",
             f"- [Front page]({SITE_BASE}/): last 24 h, flagged releases, PyPI and npm lists, all packages",
             f"- [MCP server]({SITE_BASE}/mcp/): stdio MCP wrapper (Python stdlib, source {SITE_BASE}/mcp/release_radar_mcp.py) with tools releases_today, get_package_releases and list_flagged over today.json and /pkg/*/index.json",
             f"- [Legal]({SITE_BASE}/legal/)",
             "", "## Packages", ""]
    for p in model["packages"]:
        lines.append(f"- [{p['name']} ({p['ecosystem']})]({SITE_BASE}{p['path']}): latest {p.get('latest') or '?'}" + (" · Errata correction available" if p.get("errata") else "") + f" · JSON: {SITE_BASE}{p['path']}index.json")
    lines += ["", "## CI check", "",
              f"- [ci-check.txt]({SITE_BASE}/ci-check.txt): readable copy (text/plain) of the script, identical to ci-check.py. Python 3.12 stdlib script. Reads your requirements.txt or package.json, fetches today.json and prints your dependencies released in the 48 h before `built`: package@version, date, flagged yes/no, verbatim quotes, notes URL. A flag is a regex match plus a quote; no flag does not mean safe.",
              "- Run: `curl -sO " + SITE_BASE + "/ci-check.py && python3 ci-check.py requirements.txt --fail-on-flag` (exit 0 unless --fail-on-flag and a listed release is flagged)",
              "", "## today.json release fields", "",
              f"{SITE_BASE}/today.json has top-level keys built, window_start, window_days, counts, site, disclosure and releases. Each item of `releases` has these keys:",
              "- id: `<ecosystem>/<package>/<version>`",
              "- ecosystem: `pypi` or `npm`",
              "- package, version: registry name and version string",
              "- date: UTC publication time, ISO 8601",
              "- prerelease: true if the version is a pre-release (PyPI a/b/rc/dev, npm semver `-` part) or GitHub marks it so",
              "- bump: `major`, `minor`, `patch`, `prerelease` or `unknown`; the step from the highest known stable version below this one",
              "- variant_of: base version string when this version is a platform build of another version (e.g. `1.2.3` for a variant of it), otherwise null",
              "- yanked_or_deprecated: true if the registry marks the version yanked or deprecated",
              "- flagged: true if the release notes matched the breaking regex; a regex match, not a judgement",
              "- quotes: list of `{match, quote}` objects, at most 3; `match` is the matched word, `quote` the verbatim line from the notes (≤ 280 characters); empty list if not flagged",
              "- notes_url: release notes URL; notes_redirect: the same via the site's /go/<id>/ page (null if none)",
              "- notes_kind: `github_release` (notes were read and checked), `changelog`, `repo_releases` or `registry_page` (a link only, nothing was read, so not flagged)",
              "- errata_url: URL of a verified Errata correction for the package, or null",
              "- page, json: this package's page and its index.json",
              "", "## Related", "", "- [Errata — where AI models write outdated code, and the verified fix](https://formachines.ai/errata/)",
              f"- [Errata skill for coding agents]({ERRATA_SKILL_URL})", ""]
    return "\n".join(lines)


ROBOTS_TXT = """# Release Radar — everyone is welcome, including AI crawlers and agents.
User-agent: *
Allow: /
Disallow: /go/

User-agent: GPTBot
Allow: /
Disallow: /go/

User-agent: ClaudeBot
Allow: /
Disallow: /go/

User-agent: Google-Extended
Allow: /

Sitemap: __SITE__/sitemap.xml
"""


def render_sitemap(model: dict) -> str:
    urls = [SITE_BASE + "/", SITE_BASE + "/legal/", SITE_BASE + "/brand/"] + [SITE_BASE + p["path"] for p in model["packages"]]
    return ('<?xml version="1.0" encoding="UTF-8"?>\n<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
            + "".join(f"<url><loc>{esc(u)}</loc><lastmod>{esc(model['built'][:10])}</lastmod></url>" for u in urls) + "</urlset>\n")


# ----------------------------------------------------------------------------------------------------------
# Write public/
# ----------------------------------------------------------------------------------------------------------
def write_site(model: dict, errata_count: int, http: Http) -> dict:
    global GIT_SHA
    GIT_SHA = git_head()
    built = model["built"]
    PUBLIC_DIR.mkdir(parents=True, exist_ok=True)
    # never delete public/ wholesale: addresses must survive; stale pkg pages stay until removed on purpose.
    shutil.copyfile(STATIC_DIR / "style.css", PUBLIC_DIR / "style.css")
    write_text(PUBLIC_DIR / "index.html", render_index(model, errata_count))
    write_text(PUBLIC_DIR / "404.html", render_404(built))
    write_text(PUBLIC_DIR / "legal" / "index.html", render_legal(built))
    write_text(PUBLIC_DIR / "brand" / "index.html", render_brand(built))
    for f in (STATIC_DIR / "brand").glob("*.svg"):
        shutil.copyfile(f, PUBLIC_DIR / "brand" / f.name)
    shutil.copyfile(STATIC_DIR / "ci-check.py", PUBLIC_DIR / "ci-check.py")
    shutil.copyfile(STATIC_DIR / "ci-check.py", PUBLIC_DIR / "ci-check.txt")  # same bytes; nginx serves .py as octet-stream
    write_text(PUBLIC_DIR / "mcp" / "index.html", render_mcp(built))
    for n, t in (("release_radar_mcp.py", "release_radar_mcp.py"), ("release_radar_mcp.py", "release_radar_mcp.py.txt"), ("README.md", "README.md")):
        shutil.copyfile(Path(__file__).parent / "mcp" / n, PUBLIC_DIR / "mcp" / t)
    write_text(PUBLIC_DIR / "robots.txt", ROBOTS_TXT.replace("__SITE__", SITE_BASE))
    write_text(PUBLIC_DIR / "llms.txt", render_llms(model))
    write_text(PUBLIC_DIR / "feed.xml", render_feed(model))
    write_text(PUBLIC_DIR / "feed-breaking.xml", render_breaking_feed(model))
    write_text(PUBLIC_DIR / "sitemap.xml", render_sitemap(model))
    go_map: dict[str, str] = {}
    for p in model["packages"]:
        d = PUBLIC_DIR / p["path"].strip("/")
        write_text(d / "index.html", render_package(p, model))
        write_text(d / "feed.xml", render_feed(model, p["releases"], p))
        write_json(d / "index.json", {
            "ecosystem": p["ecosystem"], "package": p["name"], "summary": p["summary"], "latest": p.get("latest"),
            "registry": p["page"], "repository": f"https://github.com/{p['repo']}" if p.get("repo") else None,
            "changelog": p.get("changelog"), "errata": p.get("errata"), "metadata_fetched": p.get("fetched"),
            "latest_stable": p.get("latest_stable"),
            "stable_in_window": sum(1 for r in p["releases"] if not r["prerelease"]),
            "prereleases_in_window": sum(1 for r in p["releases"] if r["prerelease"]),
            "page": SITE_BASE + p["path"], "built": built, "releases": [public_release(r) for r in p["releases"]],
        })
        if p.get("errata") and p["errata"].get("url"):
            go_map[go_id(p["errata"]["url"])] = p["errata"]["url"]
    for r in model["releases"]:
        if r["notes_url"]:
            go_map[r["notes_go"]] = r["notes_url"]
    for gid, url in go_map.items():
        write_text(PUBLIC_DIR / "go" / gid / "index.html", render_go(url, built))
    for r in model["releases"]:
        for d in ("up", "down"):
            write_text(PUBLIC_DIR / "go" / "vote" / vote_id(r) / d / "index.html", render_go(vote_target(r), built, vote=True))
    write_json(PUBLIC_DIR / "go" / "index.json", {"built": built, "redirects": go_map})
    flagged = [r for r in model["releases"] if r["flagged"]]
    today = {
        "site": SITE_BASE, "built": built, "window_days": WINDOW_DAYS, "window_start": model["window_start"],
        "disclosure": "Built by AI agents (agent-team) without human editing; responsible: Dmitry, formachines.ai. License: CC BY 4.0.",
        "counts": {"packages": len(model["packages"]), "releases": len(model["releases"]), "flagged": len(flagged),
                   "prereleases": sum(1 for r in model["releases"] if r["prerelease"]),
                   "with_errata": sum(1 for r in model["releases"] if r.get("errata_url"))},
        "releases": [public_release(r) for r in model["releases"]],
    }
    write_json(PUBLIC_DIR / "today.json", today)
    build_info = {
        "built": built, "commit": GIT_SHA, "generator": "build.py", "packages": len(model["packages"]),
        "releases": len(model["releases"]), "flagged": len(flagged), "redirects": len(go_map),
        "http_requests": http.requests, "github_remaining": http.github_remaining, "errors": http.errors[:50],
        "notes_coverage": {
            "releases": len(model["releases"]),
            "github_release": sum(1 for r in model["releases"] if r["notes_kind"] == "github_release"),
            "missing": group_missing(model["notes_missing"]),
            "counts": notes_counts(model["notes_missing"], sum(1 for r in model["releases"] if r.get("no_notes"))),
        },
    }
    write_json(PUBLIC_DIR / "build.json", build_info)
    return build_info


# ----------------------------------------------------------------------------------------------------------
# Main
# ----------------------------------------------------------------------------------------------------------
def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--offline", action="store_true", help="no network; build from data/ caches")
    ap.add_argument("--no-github", action="store_true", help="skip GitHub Releases API (use cached notes / fallback links)")
    ap.add_argument("--limit", type=int, default=None, help="only the first N packages")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s", datefmt="%Y-%m-%dT%H:%M:%S")
    t0 = time.time()
    log.info("build start: offline=%s no_github=%s limit=%s token=%s", args.offline, args.no_github, args.limit,
             "yes" if os.environ.get("GITHUB_TOKEN") else "no")
    http = Http(offline=args.offline)
    packages = load_packages(args.limit)
    ignore = load_ignore_patterns()
    errata = fetch_errata(http)
    model = collect(http, packages, ignore, errata, args.no_github)
    info = write_site(model, len(errata), http)
    state = {"last_build": info, "duration_s": round(time.time() - t0, 1)}
    write_json(STATE_FILE, state)
    log.info("done in %.1fs: packages=%d releases=%d flagged=%d redirects=%d requests=%d errors=%d github_remaining=%s",
             state["duration_s"], info["packages"], info["releases"], info["flagged"], info["redirects"],
             info["http_requests"], len(http.errors), info["github_remaining"])
    for e in http.errors:
        log.warning("error: %s", e)
    return 0


if __name__ == "__main__":
    sys.exit(main())

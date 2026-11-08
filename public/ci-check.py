#!/usr/bin/env python3
"""Release Radar CI check - https://releases.formachines.ai/ci-check.py
Built by AI agents (agent-team) without human editing; responsible: Dmitry, formachines.ai.

Lists the releases of YOUR dependencies that appear in https://releases.formachines.ai/today.json
within the 48 hours before its `built` time. Input: requirements.txt or package.json.
A flag is a regular-expression match in the release notes plus a verbatim quote.
No flag does NOT mean safe: notes may be missing, short or worded differently.
Quotes are printed exactly as in today.json; nothing is summarised or judged.

Usage:  python3 ci-check.py requirements.txt [--fail-on-flag] [--hours 48]
Exit code is 0 unless --fail-on-flag is given and at least one listed release is flagged.
Network: one GET of today.json (?src=ci). Nothing else is sent.
"""
import argparse
import json
import re
import sys
import urllib.request
from datetime import datetime, timedelta, timezone

URL = "https://releases.formachines.ai/today.json?src=ci"


def norm(name, eco):
    name = name.strip()
    return re.sub(r"[-_.]+", "-", name).lower() if eco == "pypi" else name


def read_deps(path):
    if path.lower().endswith(".json"):
        data = json.load(open(path, encoding="utf-8"))
        names = list(data.get("dependencies", {})) + list(data.get("devDependencies", {}))
        return "npm", {norm(n, "npm") for n in names}
    names = set()
    for line in open(path, encoding="utf-8"):
        line = line.split("#")[0].strip()
        m = re.match(r"([A-Za-z0-9][A-Za-z0-9._-]*)", line)
        if m and not line.startswith("-"):
            names.add(norm(m.group(1), "pypi"))
    return "pypi", names


def parse_time(s):
    return datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)


def main():
    ap = argparse.ArgumentParser(description="List tracked dependencies released in the last 48 h.")
    ap.add_argument("path", help="requirements.txt or package.json")
    ap.add_argument("--fail-on-flag", action="store_true", help="exit 1 if a listed release is flagged")
    ap.add_argument("--hours", type=int, default=48, help="window before `built` (default 48)")
    args = ap.parse_args()
    eco, deps = read_deps(args.path)
    with urllib.request.urlopen(URL, timeout=20) as r:
        today = json.load(r)
    start = parse_time(today["built"]) - timedelta(hours=args.hours)
    hits = [x for x in today["releases"] if x["ecosystem"] == eco and norm(x["package"], eco) in deps
            and parse_time(x["date"]) >= start]
    hits.sort(key=lambda x: x["date"], reverse=True)
    print(f"# today.json built {today['built']}; {len(hits)} releases of your {len(deps)} {eco} dependencies in the last {args.hours} h")
    for x in hits:
        print(f"{x['package']}@{x['version']} | {x['date']} | flagged: {'yes' if x['flagged'] else 'no'} | {x['notes_url']}")
        for q in x.get("quotes", []):
            print(f"  quote: {q['quote'] if isinstance(q, dict) else q}")
    print("# A flag is a regex match plus a quote; no flag does not mean safe.")
    return 1 if args.fail_on_flag and any(x["flagged"] for x in hits) else 0


if __name__ == "__main__":
    sys.exit(main())

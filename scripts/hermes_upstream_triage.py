#!/usr/bin/env python3
"""Classify upstream commits vs our fork's modified files.

Walks origin/main..main to build the set of files our fork touches.
Then walks (merge_base..origin/main) to score each upstream commit:

  safe    — touches zero fork-modified files
  docs    — safe AND only touches docs/* or *.md
  tests   — safe AND only touches tests/*
  conflict — touches any fork-modified file (needs manual merge)

Emits a table for manual review and a YAML-ish candidate list that
`git cherry-pick --no-commit` can consume for a batch test.

Usage:
    ./scripts/hermes_upstream_triage.py
    ./scripts/hermes_upstream_triage.py --limit 100 --min-safe
"""
from __future__ import annotations

import argparse
import subprocess
import sys
from collections import Counter
from pathlib import Path


REPO = Path(__file__).resolve().parents[1]


def git(*args: str) -> str:
    r = subprocess.run(
        ["git", "-C", str(REPO), *args],
        capture_output=True, text=True, check=False,
    )
    return r.stdout.strip()


def classify(files: list[str], fork_files: set[str]) -> str:
    if not files:
        return "empty"
    if any(f in fork_files for f in files):
        return "conflict"
    if all(f.startswith("docs/") or f.endswith(".md") for f in files):
        return "docs"
    if all(f.startswith("tests/") for f in files):
        return "tests"
    return "safe"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--limit", type=int, default=200,
                        help="inspect the newest N upstream commits")
    parser.add_argument("--min-safe", action="store_true",
                        help="show only safe/docs/tests")
    args = parser.parse_args()

    merge_base = git("merge-base", "main", "origin/main")
    if not merge_base:
        print("could not determine merge-base with origin/main", file=sys.stderr)
        return 1

    fork_files = set(git("diff", "--name-only", f"{merge_base}..main").split())
    print(f"Fork merge-base: {merge_base}")
    print(f"Fork-modified files: {len(fork_files)}")
    print()

    log = git(
        "log", "--reverse", "--format=%H\t%s",
        f"{merge_base}..origin/main",
        f"--max-count={args.limit}",
    )
    if not log:
        print("(no upstream commits to triage)")
        return 0

    category_tally: Counter[str] = Counter()
    lines: list[tuple[str, str, str, list[str]]] = []  # category, sha, msg, files

    for row in log.splitlines():
        sha, _, msg = row.partition("\t")
        files = git("show", "--stat=1000", "--name-only", "--format=", sha).splitlines()
        files = [f for f in files if f]
        cat = classify(files, fork_files)
        category_tally[cat] += 1
        lines.append((cat, sha, msg, files))

    print("Category tally:")
    for c, n in category_tally.most_common():
        print(f"  {c:10s} {n}")
    print()

    showable = {"safe", "docs", "tests"} if args.min_safe else {"safe", "docs", "tests", "conflict"}
    print(f"Showing {sum(1 for l in lines if l[0] in showable)} / {len(lines)} commits:")
    print()
    for cat, sha, msg, files in lines:
        if cat not in showable:
            continue
        print(f"  [{cat:8s}] {sha[:10]} {msg[:100]}")
        if cat != "conflict":
            for f in files[:3]:
                print(f"              {f}")
            if len(files) > 3:
                print(f"              (+{len(files) - 3} more)")
    print()
    print("Next step: review the `safe` list, cherry-pick a small batch,")
    print("  then run the Hermes smoke tests before merging to main.")
    return 0


if __name__ == "__main__":
    sys.exit(main())

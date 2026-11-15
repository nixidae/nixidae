#!/usr/bin/env agent-python
"""Find the upstream QEMU commit a vendored QEMU tree was copied from.

    find_qemu_base.py VENDORED_FILES CANDIDATES [N]

VENDORED_FILES: `git ls-tree -r <rev>:qemu` output. CANDIDATES: `<commit sha>
<date>` per line, newest first; the first N (default 30) are scored. Git blob
ids are content hashes, so the base is the commit with the fewest files that
differ from the vendored tree. Uses `gh auth token`.
"""

from __future__ import annotations

import pathlib
import subprocess
import sys

import requests

API = "https://api.github.com/repos/qemu/qemu"


def blobs_of_ls_tree(text: str) -> dict[str, str]:
    files = {}
    for line in text.splitlines():
        meta, path = line.split("\t", 1)
        _mode, kind, sha = meta.split()
        if kind == "blob":
            files[path] = sha
    return files


def main() -> None:
    vendored = blobs_of_ls_tree(pathlib.Path(sys.argv[1]).read_text())
    commits = [
        line.split()[0]
        for line in pathlib.Path(sys.argv[2]).read_text().splitlines()
        if line.strip()
    ]
    count = int(sys.argv[3]) if len(sys.argv) > 3 else 30
    token = subprocess.run(
        ["gh", "auth", "token"], capture_output=True, text=True, check=True
    ).stdout.strip()
    session = requests.Session()
    session.headers["Authorization"] = f"Bearer {token}"

    results = []
    for sha in commits[:count]:
        body = session.get(f"{API}/git/trees/{sha}?recursive=1", timeout=60).json()
        if body.get("truncated"):
            print(f"{sha}: tree listing truncated, skipped", file=sys.stderr)
            continue
        upstream = {e["path"]: e["sha"] for e in body["tree"] if e["type"] == "blob"}
        changed = sum(
            upstream.get(p) != s for p, s in vendored.items() if p in upstream
        )
        added = sum(p not in upstream for p in vendored)
        removed = sum(p not in vendored for p in upstream)
        results.append((changed + added + removed, sha, changed, added, removed))
    for total, sha, changed, added, removed in sorted(results)[:5]:
        print(
            f"{sha} differs in {total} files: {changed} changed, {added} only vendored, {removed} only upstream"
        )


if __name__ == "__main__":
    main()

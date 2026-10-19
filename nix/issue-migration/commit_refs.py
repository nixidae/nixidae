#!/usr/bin/env python3
"""List issue references in the commit messages of each source subtree.

Writes commit-refs.jsonl: {commit, source, bare: [N...], qualified: ["owner/repo#N"...]}.
A bare #N in a source's history means that source's own repo.
"""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

REPO = Path("/home/lillecarl/Code/nixidae")
SOURCES = ["easykubenix", "ghanix", "havsorm", "huggorm", "nanopynix", "nixkube", "pydd", "pynixd", "vivarium"]
OUT = Path(__file__).with_name("commit-refs.jsonl")
QUALIFIED = re.compile(r"\b([\w.-]+/[\w.-]+)#(\d+)\b")
BARE = re.compile(r"(?<![\w/])#(\d+)\b")

with OUT.open("w") as f:
    for src in SOURCES:
        log = subprocess.run(["git", "-C", str(REPO), "log", "--format=%H%x00%B%x01", "--", f"{src}/"],
                             capture_output=True, text=True, check=True).stdout
        for entry in log.split("\x01"):
            if "\x00" not in entry:
                continue
            commit, msg = entry.strip("\n").split("\x00", 1)
            q = [f"{m[0]}#{m[1]}" for m in QUALIFIED.findall(msg)]
            bare = sorted({int(n) for n in BARE.findall(QUALIFIED.sub("", msg))})
            if q or bare:
                f.write(json.dumps({"commit": commit, "source": src, "bare": bare, "qualified": q}) + "\n")

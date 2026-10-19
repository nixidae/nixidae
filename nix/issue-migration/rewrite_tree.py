"""pyedit script: rewrite qualified references to migrated issues in tracked files.

  pyedit -s nix/issue-migration/rewrite_tree.py             # dry run, from the repo root
  pyedit -s nix/issue-migration/rewrite_tree.py --apply

`<owner>/<source>#N`, `<source>#N` and `github.com/<owner>/<source>/issues/N`
become `nixidae/nixidae#M` or its URL through map.jsonl. A number missing from
the map is a pull request or a typo and stays as written. Bare `#N` is not touched.
"""

import json
import re
import subprocess
import sys
from collections import Counter
from pathlib import Path

ROOT = Path.cwd()
HERE = ROOT / "nix/issue-migration"
NEW = {(r["repo"], r["old"]): r["new"] for r in map(json.loads, (HERE / "map.jsonl").read_text().splitlines())}
# user-mode-nixos is vivarium's earlier name; its repository and issue numbers carried over.
ALIASES = {"user-mode-nixos": "vivarium"}
NAMES = "|".join(sorted({r for r, _ in NEW} | set(ALIASES), key=len, reverse=True))

URL = re.compile(rf"(https?://)?github\.com/(?:nixidae|Lillecarl)/({NAMES})/issues/(\d+)\b")
QUALIFIED = re.compile(rf"(?<![\w/.-])(?:nixidae|Lillecarl)/({NAMES})#(\d+)\b")
SHORT = re.compile(rf"(?<![\w/.-])({NAMES})#(\d+)\b")

stats: Counter[str] = Counter()
unmapped: Counter[str] = Counter()


def lookup(name: str, number: str, ref: str) -> int | None:
    new = NEW.get((ALIASES.get(name, name), int(number)))
    if new is None:
        unmapped[ref] += 1
    return new


def url(m: re.Match[str]) -> str:
    new = lookup(m[2], m[3], m[0])
    if new is None:
        return m[0]
    stats["url"] += 1
    return f"{m[1] or ''}github.com/nixidae/nixidae/issues/{new}"


def ref(kind: str):
    def sub(m: re.Match[str]) -> str:
        new = lookup(m[1], m[2], m[0])
        if new is None:
            return m[0]
        stats[kind] += 1
        return f"nixidae/nixidae#{new}"
    return sub


files = subprocess.run(["git", "ls-files", "-z"], capture_output=True, text=True, check=True).stdout.split("\0")
for path in filter(None, files):
    # A patch's bytes feed a patched Nix: a header edit rebuilds Nix.
    if path.startswith("nix/issue-migration/") or path.endswith((".lock", ".patch", ".png", ".svg", ".gif")):
        continue
    text = pyedit.read(path)
    if not isinstance(text, str):
        continue
    new = SHORT.sub(ref("short"), QUALIFIED.sub(ref("qualified"), URL.sub(url, text)))
    if new != text:
        pyedit.write(path, new)

print(dict(stats), file=sys.stderr)
print("unmapped:", dict(unmapped.most_common()), file=sys.stderr)

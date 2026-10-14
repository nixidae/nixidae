"""
Write the `flake.lock` of each source from nixidae's own, or check them.

A source's flake has a `nixidae` input, and builds its own directory against
its siblings there. Its other inputs, such as `nixpkgs`, carry a name the
root lock has, and take that node's revision. So a source builds the same
derivations alone as it does here. nixidae/nixidae#4.

A commit cannot name itself: its hash is not known until it exists. So the
locks name the commit under them, and land as a commit of their own that
changes nothing else. A source at that commit then builds against siblings
with the same code as its own.

    lock-subtrees            lock to HEAD, and commit the locks with jj
    lock-subtrees --check    exit 1 unless every lock names a commit that
                             only lock files have changed since

A source is a member in `nix/members.nix` with a `flake.nix`.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path
from typing import Any, Final

OWNER: Final = "nixidae"
REPO: Final = "nixidae"
LOCK: Final = "flake.lock"

type Lock = dict[str, Any]


def git(root: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True, text=True).stdout.strip()


def nix_json(expr: str) -> Any:
    out = subprocess.run(
        ["nix", "eval", "--impure", "--json", "--expr", expr], check=True, capture_output=True, text=True
    ).stdout
    return json.loads(out)


def sources(root: Path) -> list[str]:
    members = nix_json(f"builtins.attrNames (import {root}/nix/members.nix)")
    return [name for name in members if (root / name / "flake.nix").exists()]


def declared(root: Path, name: str) -> dict[str, Any]:
    """Each input of the source's flake.nix: its parsed reference, and whether it is a flake."""
    expr = (
        f"builtins.mapAttrs (_: i: {{ original = builtins.parseFlakeRef i.url; flake = i.flake or true; }})"
        f" (import {root}/{name}/flake.nix).inputs"
    )
    return nix_json(expr)


def fetched(root: Path, rev: str) -> dict[str, Any]:
    """
    The narHash and lastModified of nixidae at `rev`, from the local
    repository. A `github:` fetch of the same revision gives the same narHash.
    """
    expr = f'builtins.fetchTree {{ type = "git"; url = "file://{root}"; rev = "{rev}"; }}'
    return nix_json(f"{{ inherit ({expr}) narHash lastModified; }}")


def subtree_lock(root_lock: Lock, inputs: dict[str, Any], rev: str, meta: dict[str, Any]) -> Lock:
    root_inputs = root_lock["nodes"][root_lock["root"]]["inputs"]
    nodes: dict[str, Any] = {}
    for name, spec in inputs.items():
        if name == REPO:
            locked = {
                "lastModified": meta["lastModified"],
                "narHash": meta["narHash"],
                "owner": OWNER,
                "repo": REPO,
                "rev": rev,
                "type": "github",
            }
        elif name in root_inputs:
            locked = root_lock["nodes"][root_inputs[name]]["locked"]
        else:
            raise SystemExit(f"input {name!r} is neither nixidae nor a name in nixidae's flake.lock")
        # A flake input gets no nodes of its own inputs here: nixpkgs has none,
        # and nixidae's other pins are all `flake = false`.
        nodes[name] = {"locked": locked, "original": spec["original"]}
        if not spec["flake"]:
            nodes[name]["flake"] = False
    nodes["root"] = {"inputs": {name: name for name in inputs}}
    return {"nodes": nodes, "root": "root", "version": root_lock["version"]}


def render(lock: Lock) -> str:
    # The layout nix writes, so `nix flake lock` in a clone changes nothing.
    return json.dumps(lock, indent=2, sort_keys=True) + "\n"


def wanted(root: Path, name: str, rev: str, meta: dict[str, Any]) -> str:
    root_lock = json.loads((root / LOCK).read_text())
    return render(subtree_lock(root_lock, declared(root, name), rev, meta))


def write(root: Path) -> int:
    pending = [line for line in git(root, "status", "--porcelain").splitlines() if not line.endswith("/" + LOCK)]
    if pending:
        print("lock-subtrees: commit the work first; the locks name HEAD, and these are not in it:", file=sys.stderr)
        print("\n".join(pending), file=sys.stderr)
        return 1
    rev = git(root, "rev-parse", "HEAD")
    meta = fetched(root, rev)
    written = []
    for name in sources(root):
        path = root / name / LOCK
        text = wanted(root, name, rev, meta)
        if not path.exists() or path.read_text() != text:
            path.write_text(text)
            written.append(f"{name}/{LOCK}")
    if not written:
        print(f"lock-subtrees: every lock already names {rev[:12]}")
        return 0
    message = f"Lock the sources at nixidae {rev[:12]}\n\nWritten by lock-subtrees."
    subprocess.run(["jj", "--no-pager", "-R", str(root), "commit", "-m", message, *written], check=True)
    print(f"lock-subtrees: committed {len(written)} locks at {rev[:12]}")
    return 0


def check(root: Path, head: str) -> int:
    failed = 0
    for name in sources(root):
        path = root / name / LOCK
        if not path.exists():
            print(f"{name}: no {LOCK}")
            failed = 1
            continue
        lock = json.loads(path.read_text())
        rev = lock["nodes"][REPO]["locked"]["rev"]
        changed = git(root, "diff", "--name-only", rev, head).splitlines()
        stale = [p for p in changed if not (p.endswith("/" + LOCK) and p.count("/") == 1)]
        if stale:
            print(f"{name}: locked at {rev[:12]}, and {len(stale)} files changed since, such as {stale[0]}")
            failed = 1
        elif path.read_text() != wanted(root, name, rev, fetched(root, rev)):
            print(f"{name}: {LOCK} is not what lock-subtrees writes for {rev[:12]}")
            failed = 1
        else:
            print(f"{name}: locked at {rev[:12]}")
    if failed:
        print("Run lock-subtrees after the last change, and push the commit it makes.")
    return failed


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--check", metavar="REV", nargs="?", const="HEAD", help="check the locks at REV (HEAD)")
    parser.add_argument("--root", type=Path, default=Path.cwd())
    args = parser.parse_args()
    root = Path(git(args.root, "rev-parse", "--show-toplevel"))
    if args.check:
        return check(root, git(root, "rev-parse", args.check))
    return write(root)


if __name__ == "__main__":
    sys.exit(main())

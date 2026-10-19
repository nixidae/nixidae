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

    lock-subtrees              lock to the commit under the working copy,
                               and commit the locks with jj
    lock-subtrees --check      exit 1 unless every lock names a commit that
                               only lock files have changed since
    lock-subtrees --check REV  the same, for REV

In a jj workspace, default or not, the commit under the working copy is
`@-`, and REV is a revset. In a git checkout without jj, such as CI's, they
are HEAD and a git revision. nixidae/nixidae#18.

A source is a member in `nix/members.nix` with a `flake.nix`.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any, Final

OWNER: Final = "nixidae"
REPO: Final = "nixidae"
LOCK: Final = "flake.lock"

type Lock = dict[str, Any]


class Vcs(StrEnum):
    JJ = "jj"
    GIT = "git"


@dataclass(frozen=True)
class Workspace:
    vcs: Vcs
    root: Path
    # The git repository that holds the commits. A secondary jj workspace has
    # no `.git`; its commits are in the repository `jj git root` names.
    git_dir: Path


def run(*args: str, cwd: Path | None = None) -> str:
    done = subprocess.run(args, cwd=cwd, capture_output=True, text=True)
    if done.returncode != 0:
        raise SystemExit(f"lock-subtrees: {' '.join(args)} failed with exit {done.returncode}:\n{done.stderr.strip()}")
    return done.stdout.strip()


def jj(ws: Workspace, *args: str) -> str:
    # jj reads and prints paths relative to the current directory, not to -R.
    return run("jj", "--no-pager", "--color=never", "-R", str(ws.root), *args, cwd=ws.root)


def git(ws: Workspace, *args: str) -> str:
    return run("git", "--git-dir", str(ws.git_dir), *args)


def workspace(start: Path) -> Workspace:
    """The jj workspace or git checkout that holds `start`. The nearest marker wins."""
    start = start.resolve()
    for directory in (start, *start.parents):
        if (directory / ".jj").is_dir():
            root = Path(run("jj", "--no-pager", "-R", str(directory), "root"))
            return Workspace(Vcs.JJ, root, Path(run("jj", "--no-pager", "-R", str(root), "git", "root")))
        if (directory / ".git").exists():
            root = Path(run("git", "-C", str(directory), "rev-parse", "--show-toplevel"))
            return Workspace(Vcs.GIT, root, Path(run("git", "-C", str(root), "rev-parse", "--absolute-git-dir")))
    raise SystemExit(f"lock-subtrees: {start} is in no jj workspace and no git checkout: no .jj or .git above it")


def one_commit(revset: str, out: str) -> str:
    commits = out.splitlines()
    if len(commits) != 1:
        raise SystemExit(f"lock-subtrees: {revset} is {len(commits)} commits; the locks name exactly one")
    return commits[0]


def resolve(ws: Workspace, rev: str) -> str:
    """The commit id of `rev`: a revset in a jj workspace, a git revision in a git checkout."""
    if ws.vcs is Vcs.JJ:
        return one_commit(rev, jj(ws, "log", "--no-graph", "-r", rev, "-T", 'commit_id ++ "\\n"'))
    return git(ws, "rev-parse", "--verify", f"{rev}^{{commit}}")


def head(ws: Workspace) -> str:
    """The commit under the working copy."""
    return resolve(ws, "@-" if ws.vcs is Vcs.JJ else "HEAD")


def uncommitted(ws: Workspace) -> list[str]:
    """Changes the working copy holds over `head`, other than locks."""
    if ws.vcs is Vcs.JJ:
        changed = jj(ws, "diff", "-r", "@", "--name-only").splitlines()
    else:
        changed = run("git", "-C", str(ws.root), "status", "--porcelain").splitlines()
    return [line for line in changed if not line.endswith("/" + LOCK)]


def nix_json(expr: str) -> Any:
    return json.loads(run("nix", "eval", "--impure", "--json", "--expr", expr))


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


def fetched(ws: Workspace, rev: str) -> dict[str, Any]:
    """
    The narHash and lastModified of nixidae at `rev`, from the local
    repository. A `github:` fetch of the same revision gives the same narHash.
    """
    expr = f'builtins.fetchTree {{ type = "git"; url = "file://{ws.git_dir}"; rev = "{rev}"; }}'
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


def write(ws: Workspace) -> int:
    if ws.vcs is not Vcs.JJ:
        print(f"lock-subtrees: {ws.root} is a git checkout without jj, and the locks are committed with jj", file=sys.stderr)
        return 1
    pending = uncommitted(ws)
    if pending:
        print("lock-subtrees: commit the work first; the locks name @-, and these are in @:", file=sys.stderr)
        print("\n".join(pending), file=sys.stderr)
        return 1
    rev = head(ws)
    meta = fetched(ws, rev)
    written = []
    for name in sources(ws.root):
        path = ws.root / name / LOCK
        text = wanted(ws.root, name, rev, meta)
        if not path.exists() or path.read_text() != text:
            path.write_text(text)
            written.append(f"{name}/{LOCK}")
    if not written:
        print(f"lock-subtrees: every lock already names {rev[:12]}")
        return 0
    message = f"Lock the sources at nixidae {rev[:12]}\n\nWritten by lock-subtrees."
    jj(ws, "commit", "-m", message, *written)
    print(f"lock-subtrees: committed {len(written)} locks at {rev[:12]}")
    return 0


def check(ws: Workspace, at: str) -> int:
    failed = 0
    for name in sources(ws.root):
        path = ws.root / name / LOCK
        if not path.exists():
            print(f"{name}: no {LOCK}")
            failed = 1
            continue
        lock = json.loads(path.read_text())
        rev = lock["nodes"][REPO]["locked"]["rev"]
        changed = git(ws, "diff", "--name-only", rev, at).splitlines()
        stale = [p for p in changed if not (p.endswith("/" + LOCK) and p.count("/") == 1)]
        if stale:
            print(f"{name}: locked at {rev[:12]}, and {len(stale)} files changed since, such as {stale[0]}")
            failed = 1
        elif path.read_text() != wanted(ws.root, name, rev, fetched(ws, rev)):
            print(f"{name}: {LOCK} is not what lock-subtrees writes for {rev[:12]}")
            failed = 1
        else:
            print(f"{name}: locked at {rev[:12]}")
    if failed:
        print("Run lock-subtrees after the last change, and push the commit it makes.")
    return failed


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument(
        "--check", metavar="REV", nargs="?", const="", help="check the locks at REV (the commit under the working copy)"
    )
    parser.add_argument("--root", type=Path, default=Path.cwd())
    args = parser.parse_args()
    ws = workspace(args.root)
    if args.check is not None:
        return check(ws, resolve(ws, args.check) if args.check else head(ws))
    return write(ws)


if __name__ == "__main__":
    sys.exit(main())

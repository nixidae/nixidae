"""Build every project of a test set in each mode, and compare the modes.

Each group of modes is one way to build a source node, so every mode of a
group must give one environment, and one root around it. stdenv strips and
patches what it builds, so the stdenv modes are a group of their own.

    test-set projects [name ...]
    test-set live [name ...]

Projects run in parallel, `PYDD_JOBS` at a time (half the cores by default).
The modes of one project run in order, so `huggorm` and `drv` find the nodes
that `cli` built instead of building them beside it. Each mode logs to
`PYDD_LOG/<project>-<mode>.log`.
"""

from __future__ import annotations

import json
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from subprocess import PIPE

import anyio

#: The runner of each mode: the Nix it needs and the store it builds in.
RUNNERS = {
    "cli": "PYDD_RUN",
    "huggorm": "PYDD_RUN",
    "drv": "PYDD_RUN",
    "stdenv": "PYDD_RUN",
    "recursive": "PYDD_RUN_RECURSIVE",
    "recursive-eval": "PYDD_RUN_RECURSIVE",
    "recursive-nixpkgs": "PYDD_RUN_RECURSIVE",
    "out": "PYDD_RUN_RELEASED",
    "out-stdenv": "PYDD_RUN_RELEASED",
}

#: The groups of each set. A mode in `RELEASED` runs only for a project that
#: the set has under `out`.
GROUPS = {
    "projects": {
        "plain": ["cli", "huggorm", "drv", "recursive", "recursive-eval", "out"],
        "stdenv": ["recursive-nixpkgs", "stdenv", "out-stdenv"],
    },
    "live": {
        "live": ["cli", "huggorm", "recursive", "out"],
    },
}
RELEASED = {"out", "out-stdenv"}


@dataclass
class TestSet:
    name: str
    log: Path
    released: set[str]
    failed: bool = False

    def say(self, line: str) -> None:
        print(line, flush=True)

    async def mode(self, project: str, mode: str) -> tuple[str, str] | None:
        """Build the check, the environment and the root, from one evaluation."""
        log = self.log / f"{project}-{mode}.log"
        with log.open("wb") as stderr:
            result = await anyio.run_process(
                [os.environ[RUNNERS[mode]], f"{self.name}.{mode}.{project}", "out", "environment", "root"],
                stdout=PIPE,
                stderr=stderr,
                check=False,
                env={**os.environ, "PYDD_PRINT_PATH": "1"},
            )
        paths = result.stdout.decode().split()
        if result.returncode != 0 or len(paths) != 3:
            self.say(f"FAIL  {project} ({mode})  ({log})")
            self.failed = True
            return None
        self.say(f"pass  {project} ({mode})")
        return paths[1], paths[2]

    async def project(self, project: str, limiter: anyio.CapacityLimiter) -> None:
        async with limiter:
            for group, modes in GROUPS[self.name].items():
                modes = [mode for mode in modes if mode not in RELEASED or project in self.released]
                built = {mode: await self.mode(project, mode) for mode in modes}
                if None in built.values():
                    continue
                if len(set(built.values())) == 1:
                    environment, root = next(iter(built.values()))
                    self.say(f"same  {project}  {environment} {root}  ({group}: {' '.join(modes)})")
                else:
                    shown = " ".join(f"{mode}={' '.join(paths)}" for mode, paths in built.items())
                    self.say(f"DIFFER  {project}  ({group})  {shown}")
                    self.failed = True


async def attribute_names(attribute: str) -> list[str]:
    result = await anyio.run_process(
        ["nix", "eval", "--json", "--file", ".", attribute, "--apply", "builtins.attrNames"]
    )
    return json.loads(result.stdout)


async def main(name: str, projects: list[str]) -> int:
    cache = Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache")
    log = Path(os.environ.get("PYDD_LOG") or cache / "pydd" / name)
    log.mkdir(parents=True, exist_ok=True)
    test_set = TestSet(name, log, set(await attribute_names(f"{name}.out")))
    projects = projects or await attribute_names(f"{name}.cli")
    limiter = anyio.CapacityLimiter(int(os.environ.get("PYDD_JOBS") or (os.cpu_count() or 2) // 2 or 1))
    async with anyio.create_task_group() as group:
        for project in projects:
            group.start_soon(test_set.project, project, limiter)
    return 1 if test_set.failed else 0


if __name__ == "__main__":
    if len(sys.argv) < 2 or sys.argv[1] not in GROUPS:
        sys.exit(f"usage: test-set {{{'|'.join(GROUPS)}}} [name ...]")
    try:
        sys.exit(anyio.run(main, sys.argv[1], sys.argv[2:]))
    except KeyboardInterrupt:
        sys.exit(130)

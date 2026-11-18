"""What a guest sees of the host's store when its spec names `storePaths`.

A backend picks the form (`Backend.store`) and `Machine` only adds paths
and removes it:

- `BoundStore`: a store view (`storeview`), served as the guest's `/nix`.
- `ContainerStore`: the writable form, whose new paths a container guest
  mounts from inside.
- `AllowList`: a file of paths that libkrun's share of the whole host
  store shows and nothing else. It needs no bind mount, which a host
  without mount namespaces (macOS) lacks.
"""

from __future__ import annotations

import subprocess
import sys
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Protocol

import anyio

from . import container, storeview

if TYPE_CHECKING:
    from pathlib import Path

    from .machine_spec import MachineSpec


class StoreError(RuntimeError):
    """A path could not be put into a guest's store."""


class Store(Protocol):
    def serve(self, spec: MachineSpec) -> MachineSpec:
        """*spec*, pointed at this store."""
        ...

    async def add(self, paths: list[str]) -> list[str]:
        """Show the guest *paths*, a whole closure; return those that were new."""
        ...

    def remove(self) -> None: ...


@dataclass(frozen=True)
class BoundStore:
    root: Path
    """What the backend serves as the guest's `/nix`."""

    @classmethod
    def build(cls, root: Path, paths: list[str]) -> BoundStore:
        return cls(storeview.build(root, paths))

    def serve(self, spec: MachineSpec) -> MachineSpec:
        return replace(spec, store=str(self.root))

    async def add(self, paths: list[str]) -> list[str]:
        return storeview.add(self.root, paths)

    def remove(self) -> None:
        storeview.remove(self.root)


@dataclass(frozen=True)
class ContainerStore:
    root: Path
    """What the launcher binds in as the guest's `/nix`."""
    state: Path
    """crun's state directory, where the guest's init writes its pid."""

    @classmethod
    def build(cls, root: Path, paths: list[str], state: Path) -> ContainerStore:
        return cls(storeview.build(root, paths, writable=True), state)

    def serve(self, spec: MachineSpec) -> MachineSpec:
        return replace(spec, store=str(self.root))

    async def add(self, paths: list[str]) -> list[str]:
        added = storeview.add(self.root, paths, writable=True)
        mounts = [path for path in added if not await anyio.Path(path).is_symlink()]
        if mounts:
            await self._attach(mounts)
        return added

    async def _attach(self, paths: list[str]) -> None:
        """Mount *paths* inside the guest; see `container.attach`."""
        pid = (await anyio.Path(self.state / container.INIT_PID).read_text()).strip()
        done = await anyio.run_process(
            [sys.executable, "-m", "vivarium_runner.crun_launch", "attach", "--pid", pid, *paths],
            check=False,
            stderr=subprocess.STDOUT,
        )
        if done.returncode != 0:
            raise StoreError(f"attaching store paths failed:\n{done.stdout.decode(errors='replace')}")

    def remove(self) -> None:
        storeview.remove(self.root)


@dataclass(frozen=True)
class AllowList:
    path: Path
    """One store path per line."""

    @classmethod
    def write(cls, path: Path, paths: list[str]) -> AllowList:
        path.write_text("".join(f"{entry}\n" for entry in paths))
        return cls(path)

    def serve(self, spec: MachineSpec) -> MachineSpec:
        return replace(spec, store_allowlist=self.path)

    async def add(self, paths: list[str]) -> list[str]:
        listed = set(storeview.read_paths(self.path))
        added = [path for path in paths if path not in listed]
        with self.path.open("a") as allowlist:
            allowlist.writelines(f"{path}\n" for path in added)
        return added

    def remove(self) -> None:
        """Nothing to undo: the file goes with the run directory."""

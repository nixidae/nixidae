"""The client half of the warm-server phase (nixidae/nixidae#738).

Runs on the client guest. The server guest runs `huggorm.server SOCKET
--experimental-callbacks` as a service; each step reaches it through
OpenSSH Unix socket forwarding (`huggorm.ssh.socket_dialer`), and prints
one JSON document.

    first  mount the tree, evaluate one file, detach, leave
    again  claim the state, mount again, evaluate the rest
    drop   claim the state, mount an accessor that signals before a read
           and then waits, and report how the evaluation ends once the
           host breaks the link
"""

from __future__ import annotations

import argparse
import json
import pathlib
import sys
import time
from dataclasses import asdict, dataclass, field
from typing import Any

import anyio

from huggorm import remote, ssh
from huggorm_bindings import FileType, SourceAccessor, Stat, Store, filesystem_accessor
from huggorm_generated import AsyncEvalState, AsyncStore

DUMMY = "dummy://?read-only=false"
# The link is dead after INTERVAL * COUNT seconds without an answer.
KEEPALIVE_INTERVAL = 2
KEEPALIVE_COUNT = 3


@dataclass(frozen=True, slots=True)
class Target:
    server: str
    socket: str
    key: pathlib.Path
    root: pathlib.Path
    store: str

    def dialer(self) -> Any:
        # Any: huggorm.transport.Dialer, a callable alias.
        return ssh.socket_dialer(
            self.server,
            self.socket,
            username="root",
            client_keys=[str(self.key)],
            known_hosts=None,
            keepalive_interval=KEEPALIVE_INTERVAL,
            keepalive_count_max=KEEPALIVE_COUNT,
        )

    def path(self) -> Any:
        return Store(DUMMY).add_accessor_to_store("source", filesystem_accessor(self.root))


class Logged(SourceAccessor):
    """`filesystem_accessor(root)`, with each file read recorded and
    every hook counted."""

    def __init__(self, root: pathlib.Path) -> None:
        super().__init__(filesystem_accessor(root))
        self.files: list[str] = []
        self.calls = 0

    def maybe_lstat(self, path: str) -> Stat | None:
        self.calls += 1
        return super().maybe_lstat(path)

    def read_directory(self, path: str) -> dict[str, FileType | None]:
        self.calls += 1
        return super().read_directory(path)

    def read_link(self, path: str) -> str:
        self.calls += 1
        return super().read_link(path)

    def read_file(self, path: str) -> bytes:
        self.calls += 1
        self.files.append(path)
        return super().read_file(path)


class Stalls:
    """An async accessor whose `read_file` of `stall` writes `marker`
    and then waits forever: the host breaks the link meanwhile."""

    def __init__(self, root: pathlib.Path, stall: str, marker: pathlib.Path) -> None:
        self.below = filesystem_accessor(root)
        self.stall = stall
        self.marker = marker

    async def maybe_lstat(self, path: str) -> Stat | None:
        return self.below.maybe_lstat(path)

    async def read_directory(self, path: str) -> dict[str, FileType | None]:
        return self.below.read_directory(path)

    async def read_link(self, path: str) -> str:
        return self.below.read_link(path)

    async def read_file(self, path: str) -> bytes:
        if path == self.stall:
            await anyio.Path(self.marker).write_text("reading\n")
            # The host breaks the link now; nothing answers this read.
            await anyio.sleep_forever()
        return self.below.read_file(path)


@dataclass(slots=True)
class Report:
    token: str | None = None
    state: str = ""
    mounted_at: str = ""
    value: Any = None  # Any: the project's own JSON, passed through.
    files: list[str] = field(default_factory=list)
    error_type: str = ""
    error: str = ""
    seconds: float = 0.0


async def first(target: Target) -> Report:
    report = Report()
    tree = Logged(target.root)
    async with remote.connect(target.dialer(), experimental_callbacks=True) as client:
        store = await client.acquire(AsyncStore, target.store)
        state = await client.acquire(AsyncEvalState, store)
        mounted = await state.mount(tree, "source", target.path())
        report.mounted_at = await store.print_store_path(await mounted.path())
        report.value = await (await state.eval_file(f"{report.mounted_at}/sub/x.nix")).integer()
        if not await client.detach(all=True):
            raise RuntimeError("detach moved no lease")
        report.token, report.state = client.token, remote.handle_of(state)
    report.files = tree.files
    return report


async def again(target: Target, token: str, state_id: str) -> Report:
    report = Report(token=token, state=state_id)
    tree = Logged(target.root)
    async with remote.connect(target.dialer(), claim=token, experimental_callbacks=True) as client:
        store = await client.acquire(AsyncStore, target.store)
        state = client.proxy(AsyncEvalState, state_id)
        mounted = await state.mount(tree, "source", target.path())
        report.mounted_at = await store.print_store_path(await mounted.path())
        value = await state.eval_file(f"{report.mounted_at}/default.nix")
        report.value = json.loads(await value.to_json())
        await client.detach(all=True)
    report.files = tree.files
    return report


async def drop(target: Target, token: str, state_id: str, marker: pathlib.Path) -> Report:
    report = Report(token=token, state=state_id)
    started = time.monotonic()
    try:
        async with remote.connect(target.dialer(), claim=token, experimental_callbacks=True) as client:
            store = await client.acquire(AsyncStore, target.store)
            state = client.proxy(AsyncEvalState, state_id)
            # A file no earlier step read: the mount keeps every answer,
            # so only an unread file reaches the client.
            mounted = await state.mount(Stalls(target.root, "/unread.txt", marker), "source", target.path())
            root = await store.print_store_path(await mounted.path())
            started = time.monotonic()
            await state.eval_expr(f"builtins.readFile {root}/unread.txt")
    except Exception as error:  # noqa: BLE001 -- how it ends is the result
        report.error_type = type(error).__name__
        report.error = str(error)
    report.seconds = time.monotonic() - started
    return report


@dataclass(slots=True)
class Timed:
    seconds: float
    callbacks: int


@dataclass(slots=True)
class Sweep:
    steps: dict[str, Timed] = field(default_factory=dict)


# Each pass parses anew, so the reads in it run again: what code that
# re-applies a function over the tree does on a warm state.
RERUN = "[ (builtins.readDir ./sub) (builtins.readFile ./hello.txt) (builtins.pathExists ./absent) {n} ]"


async def sweep(target: Target) -> Sweep:
    """One warm connection, each operation timed with the callbacks it made."""
    report = Sweep()
    tree = Logged(target.root)

    def took(name: str, started: float, before: int) -> None:
        report.steps[name] = Timed(time.monotonic() - started, tree.calls - before)

    path = target.path()
    started = time.monotonic()
    async with remote.connect(target.dialer(), experimental_callbacks=True) as client:
        took("connect", started, 0)
        started = time.monotonic()
        store = await client.acquire(AsyncStore, target.store)
        state = await client.acquire(AsyncEvalState, store)
        took("acquire", started, 0)
        started = time.monotonic()
        root = await store.print_store_path(await (await state.mount(tree, "source", path)).path())
        took("mount", started, 0)
        for name, expr in (
            ("cold", "builtins.toJSON (import ./default.nix)"),
            ("warm", "builtins.toJSON (import ./default.nix)"),
            ("rerun-1", RERUN.format(n=1)),
            ("rerun-2", RERUN.format(n=2)),
        ):
            before, started = tree.calls, time.monotonic()
            await (await state.eval_expr(expr, root)).to_json()
            took(name, started, before)
        # A second state, mounted with the tree's listing: only file
        # contents cross the link.
        listing = filesystem_accessor(target.root).list_nar("/", True)
        listed = await client.acquire(AsyncEvalState, store)
        before, started = tree.calls, time.monotonic()
        await listed.mount(tree, "source", path, listing)
        took("mount+listing", started, before)
        before, started = tree.calls, time.monotonic()
        await (await listed.eval_expr("builtins.toJSON (import ./default.nix)", root)).to_json()
        took("cold+listing", started, before)
    return report


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("step", choices=["first", "again", "drop", "sweep"])
    parser.add_argument("--server", required=True)
    parser.add_argument("--socket", required=True)
    parser.add_argument("--key", type=pathlib.Path, required=True)
    parser.add_argument("--root", type=pathlib.Path, required=True)
    parser.add_argument("--store", default=DUMMY)
    parser.add_argument("--token")
    parser.add_argument("--state")
    parser.add_argument("--marker", type=pathlib.Path)
    args = parser.parse_args()
    target = Target(args.server, args.socket, args.key, args.root, args.store)
    report: Report | Sweep
    match args.step:
        case "sweep":
            report = anyio.run(sweep, target)
        case "first":
            report = anyio.run(first, target)
        case "again":
            report = anyio.run(again, target, args.token, args.state)
        case _:
            report = anyio.run(drop, target, args.token, args.state, args.marker)
    json.dump(asdict(report), sys.stdout)


if __name__ == "__main__":
    main()

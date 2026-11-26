"""The client half of the remote-evaluation test (nixidae/nixidae#738).

Runs on the client guest. Opens SSH to the server, starts
`huggorm.server --stdio --experimental-callbacks` there, mounts ROOT on
the server's EvalState and evaluates `default.nix` from it. Prints one
JSON document: the values, the reads per hook and the timings.
"""

from __future__ import annotations

import argparse
import collections
import json
import pathlib
import posixpath
import sys
import time
from collections.abc import Iterable
from dataclasses import asdict, dataclass, field
from typing import Any

import anyio
import asyncssh

from huggorm import ServerOptions, remote, ssh
from huggorm_bindings import FileType, SourceAccessor, Stat, Store, filesystem_accessor
from huggorm_generated import AsyncEvalState, AsyncStore

# Hashing needs no store: the client computes the mount's store path itself.
DUMMY = "dummy://?read-only=false"


def visible(files: Iterable[str]) -> frozenset[str]:
    """Each file, as a path in the tree (`/a/b`), and every directory above it."""
    shown = {"/"}
    for name in files:
        path = posixpath.join("/", name)
        while path not in shown:
            shown.add(path)
            path = posixpath.dirname(path)
    return frozenset(shown)


class Tracked(SourceAccessor):
    """`filesystem_accessor(root)` cut down to `shown`, or all of it when
    `shown` is None. Counts the calls to each hook."""

    def __init__(self, root: pathlib.Path, shown: frozenset[str] | None) -> None:
        super().__init__(filesystem_accessor(root))
        self.shown = shown
        self.calls: collections.Counter[str] = collections.Counter()
        self.hidden_asked: set[str] = set()

    def _shows(self, path: str) -> bool:
        if self.shown is None or path in self.shown:
            return True
        self.hidden_asked.add(path)
        return False

    def maybe_lstat(self, path: str) -> Stat | None:
        self.calls["maybe_lstat"] += 1
        return super().maybe_lstat(path) if self._shows(path) else None

    def read_directory(self, path: str) -> dict[str, FileType | None]:
        self.calls["read_directory"] += 1
        entries = super().read_directory(path)
        if self.shown is None:
            return entries
        return {name: kind for name, kind in entries.items() if posixpath.join(path, name) in self.shown}

    def read_link(self, path: str) -> str:
        self.calls["read_link"] += 1
        if not self._shows(path):
            raise FileNotFoundError(path)
        return super().read_link(path)

    def read_file(self, path: str) -> bytes:
        self.calls["read_file"] += 1
        if not self._shows(path):
            raise FileNotFoundError(path)
        return super().read_file(path)


@dataclass(frozen=True, slots=True)
class Options:
    server: str
    port: int
    key: pathlib.Path
    python: str
    root: pathlib.Path
    files: pathlib.Path | None
    store: str
    expr: str
    escape: str | None
    settings: dict[str, str]
    repeat: int
    load_config: bool


@dataclass(slots=True)
class Pass:
    seconds: float
    calls: dict[str, int]


@dataclass(slots=True)
class Report:
    mounted_at: str = ""
    shown_files: int | None = None
    value: Any = None  # Any: the project's own JSON, passed through.
    escape_error: str = ""
    hidden_asked: list[str] = field(default_factory=list)
    calls_mount: dict[str, int] = field(default_factory=dict)
    passes: list[Pass] = field(default_factory=list)
    seconds: dict[str, float] = field(default_factory=dict)


def shown_by(files: pathlib.Path | None) -> frozenset[str] | None:
    if files is None:
        return None
    return visible(line for line in files.read_text().splitlines() if line)


async def evaluate(state: AsyncEvalState, tree: Tracked, expr: str, base: str) -> tuple[Pass, Any]:
    tree.calls.clear()
    started = time.monotonic()
    value = json.loads(await (await state.eval_expr(expr, base)).to_json())
    return Pass(time.monotonic() - started, dict(tree.calls)), value


async def run(options: Options) -> Report:
    report = Report()
    shown = shown_by(options.files)
    report.shown_files = None if shown is None else len(shown)
    started = time.monotonic()
    local = Store(DUMMY).add_accessor_to_store("source", Tracked(options.root, shown))
    report.seconds["hash_locally"] = time.monotonic() - started
    command = ssh.server_command(ServerOptions(callbacks=True, load_config=options.load_config), python=options.python)
    started = time.monotonic()
    async with (
        asyncssh.connect(
            options.server, options.port, username="root", client_keys=[str(options.key)], known_hosts=None
        ) as connection,
        ssh.spawned(connection, command) as dial,
        remote.connect(dial, experimental_callbacks=True) as client,
    ):
        store = await client.acquire(AsyncStore, options.store)
        state = await client.acquire(AsyncEvalState, store, options.settings)
        report.seconds["connect"] = time.monotonic() - started

        tree = Tracked(options.root, shown)
        started = time.monotonic()
        at = await (await state.mount(tree, "source", local)).path()
        report.seconds["mount"] = time.monotonic() - started
        report.calls_mount = dict(tree.calls)
        report.mounted_at = await store.print_store_path(at)

        # Each pass a fresh EvalState over the same mount would be cold
        # again; the same state shows what its caches save.
        for _ in range(options.repeat):
            done, report.value = await evaluate(state, tree, options.expr, report.mounted_at)
            report.passes.append(done)

        if options.escape is not None:
            try:
                await state.eval_expr(options.escape)
            except Exception as error:  # noqa: BLE001 -- the error text is the result
                report.escape_error = str(error)
        report.hidden_asked = sorted(tree.hidden_asked)
    return report


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--server", required=True)
    parser.add_argument("--port", type=int, default=22)
    parser.add_argument("--key", type=pathlib.Path, required=True)
    parser.add_argument("--python", required=True, help="the interpreter on the server")
    parser.add_argument("--root", type=pathlib.Path, required=True)
    parser.add_argument("--files", type=pathlib.Path, help="the files to show, one relative path a line")
    parser.add_argument("--store", default="daemon", help="the store URI the server opens")
    parser.add_argument("--expr", default="import ./default.nix", help="evaluated in the mounted tree")
    parser.add_argument("--escape", help="an expression that must fail on the server")
    parser.add_argument("--setting", nargs=2, action="append", default=[], metavar=("NAME", "VALUE"))
    parser.add_argument("--repeat", type=int, default=1)
    parser.add_argument("--load-config", action="store_true", help="the server reads its own nix.conf")
    args = parser.parse_args()
    options = Options(
        server=args.server,
        port=args.port,
        key=args.key,
        python=args.python,
        root=args.root,
        files=args.files,
        store=args.store,
        expr=args.expr,
        escape=args.escape,
        settings=dict(args.setting),
        repeat=args.repeat,
        load_config=args.load_config,
    )
    json.dump(asdict(anyio.run(run, options)), sys.stdout)


if __name__ == "__main__":
    main()

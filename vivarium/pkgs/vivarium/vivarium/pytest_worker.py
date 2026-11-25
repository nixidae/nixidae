"""The guest's end of a test that runs in a guest (`vivarium.pytest_host`).

The host unpacks its project at the same path in the guest, puts its own
Python in the guest's store view, and starts:

    python -m vivarium.pytest_worker SOCKET LOG [--coverage FILE ...] -- PYTEST-ARGS... NODEIDS...

This is plain pytest with one more plugin, kept alive: it collects once,
then runs one test each time the host asks. The host reaches it through
the agent (`Machine.relay`), which calls the arpyc service on SOCKET:

    collect()           -> JSON {"items": [nodeid, ...], "reports": [...]}
    run(nodeid, nextid) -> JSON [report, ...]
    stop()              -> pytest's exit code, once the session has ended

A report is in the form xdist sends between processes
(`pytest_report_to_serializable`). The host turns each back into a report
and hands it to its own pytest, so its reporters see an ordinary test.
`nextid` is the test the host runs after this one, so pytest tears down a
fixture only when no later test shares it, as it does in one process.

The worker listens on SOCKET before it leaves the host's command, so the
host's first call can never come before the socket exists: a call that
comes early waits in the listen queue. It then runs on in a session of its
own, its output in LOG, until `stop`.

With `--coverage`, coverage.py measures the run and saves to FILE before
`stop` answers. It starts before pytest collects, so a project module is
measured from its import. The host puts FILE beside pytest-cov's data
file, whose combine merges it: the paths match, because the project is at
its own path.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import socket
import sys
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING, Any

import anyio
import coverage
import pytest
from anyio.abc import SocketAttribute, SocketListener
from anyio.from_thread import start_blocking_portal
from vivarium_agent.arpyc import Service, listen

if TYPE_CHECKING:
    from collections.abc import Iterator

    from anyio.abc import SocketStream, TaskStatus
    from anyio.from_thread import BlockingPortal
    from anyio.streams.memory import MemoryObjectReceiveStream, MemoryObjectSendStream


STOP_WAIT = 30.0
"""Seconds for the agent to take `stop`'s answer and close its connection."""


class Kind(StrEnum):
    COLLECT = "collect"
    RUN = "run"
    STOP = "stop"


@dataclass
class Request:
    """One call of the host's, handed from the service's loop to pytest's thread."""

    kind: Kind
    nodeid: str | None = None
    nextid: str | None = None
    done: anyio.Event = field(default_factory=anyio.Event)
    answer: object = None
    error: BaseException | None = None


class Recorder:
    """Keeps every test report, and each collection report that failed,
    until the host takes them."""

    def __init__(self) -> None:
        self.config: pytest.Config | None = None
        # The serialized form has no fixed shape: it is pytest's own.
        self.reports: list[dict[str, Any]] = []

    def pytest_configure(self, config: pytest.Config) -> None:
        self.config = config
        # The host's plugin reads the mark; here it is known, so `--strict-markers` holds.
        config.addinivalue_line("markers", "vivarium: read by the host's pytest, which ran this test here")

    def _keep(self, report: pytest.TestReport | pytest.CollectReport) -> None:
        if self.config is None:
            raise RuntimeError("a report before pytest_configure")
        self.reports.append(self.config.hook.pytest_report_to_serializable(config=self.config, report=report))

    def pytest_runtest_logreport(self, report: pytest.TestReport) -> None:
        self._keep(report)

    def pytest_collectreport(self, report: pytest.CollectReport) -> None:
        if report.failed:
            self._keep(report)

    def take(self) -> list[dict[str, Any]]:
        taken, self.reports = self.reports, []
        return taken


class Loop:
    """pytest's run loop, with the host choosing each test."""

    def __init__(self, portal: BlockingPortal, inbox: MemoryObjectReceiveStream[Request], recorder: Recorder) -> None:
        self.portal = portal
        self.inbox = inbox
        self.recorder = recorder
        self.stop: Request | None = None

    def answer(self, request: Request, answer: object = None, error: BaseException | None = None) -> None:
        request.answer, request.error = answer, error
        self.portal.call(request.done.set)

    def _run(self, items: dict[str, pytest.Item], request: Request) -> str:
        item = items.get(request.nodeid or "")
        if item is None:
            raise LookupError(f"the guest's pytest did not collect {request.nodeid}")
        self.recorder.take()
        nextitem = items.get(request.nextid) if request.nextid else None
        item.config.hook.pytest_runtest_protocol(item=item, nextitem=nextitem)
        return json.dumps(self.recorder.take())

    @pytest.hookimpl(tryfirst=True)
    def pytest_runtestloop(self, session: pytest.Session) -> bool:
        """Never reads `session.shouldstop`: `-x` and `--maxfail` are the
        host's to apply, by asking for no more tests."""
        items = {item.nodeid: item for item in session.items}
        while True:
            request = self.portal.call(self.inbox.receive)
            if request.kind is Kind.STOP:
                self.stop = request
                return True
            try:
                if request.kind is Kind.COLLECT:
                    answer = json.dumps({"items": list(items), "reports": self.recorder.take()})
                else:
                    answer = self._run(items, request)
            except Exception as error:  # noqa: BLE001 -- the host's call fails with it; the loop goes on
                self.answer(request, error=error)
            else:
                self.answer(request, answer)


class Worker(Service):
    """What the host may ask, through the agent's relay."""

    def __init__(self, outbox: MemoryObjectSendStream[Request]) -> None:
        self.outbox = outbox
        self.accepting: anyio.CancelScope | None = None
        """Set by `_serve`, on the loop: a scope belongs to the loop it is made on."""

    async def _ask(self, request: Request) -> object:
        await self.outbox.send(request)
        await request.done.wait()
        if request.error is not None:
            raise request.error
        return request.answer

    async def exposed_collect(self) -> object:
        return await self._ask(Request(Kind.COLLECT))

    async def exposed_run(self, nodeid: str, nextid: str | None = None) -> object:
        return await self._ask(Request(Kind.RUN, nodeid, nextid))

    async def exposed_stop(self) -> object:
        return await self._ask(Request(Kind.STOP))


async def _client(stream: SocketStream, worker: Worker) -> None:
    # arpyc reads a raw fd from the loop; the duplicate outlives the stream.
    fd = os.dup(stream.extra(SocketAttribute.raw_socket).fileno())
    await stream.aclose()
    await listen(fd, worker, raw_tty=False).serve_forever()


async def _serve(server: socket.socket, worker: Worker, *, task_status: TaskStatus[None]) -> None:
    """Serve each connection until `worker.accepting` is cancelled, then
    wait for those open to end: the agent closes each once it has its answer."""
    listener = await SocketListener.from_socket(server)
    async with anyio.create_task_group() as clients, listener:
        with anyio.CancelScope() as worker.accepting:
            task_status.started()
            while True:
                clients.start_soon(_client, await listener.accept(), worker)


def parser() -> argparse.ArgumentParser:
    top = argparse.ArgumentParser(prog="python -m vivarium.pytest_worker")
    top.add_argument("socket", type=Path, help="where the worker serves the host's calls")
    top.add_argument("log", type=Path, help="where its output goes once it leaves the host's command")
    top.add_argument("--coverage", type=Path, help="measure the run with coverage.py, saved here")
    top.add_argument("--cov-source", action="append", help="as pytest-cov's --cov, once each")
    top.add_argument("--cov-branch", action="store_true")
    top.add_argument("--cov-config", default=".coveragerc")
    return top


@contextlib.contextmanager
def measured(args: argparse.Namespace) -> Iterator[None]:
    if args.coverage is None:
        yield
        return
    cov = coverage.Coverage(
        data_file=str(args.coverage),
        source=args.cov_source,
        branch=args.cov_branch or None,
        config_file=args.cov_config,
    )
    cov.start()
    try:
        yield
    finally:
        cov.stop()
        cov.save()


def _bind(path: Path) -> socket.socket:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.unlink(missing_ok=True)
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.bind(str(path))
    server.listen()
    return server


def _detach(log: Path) -> None:
    """Leave the host's command: it returns once this process's parent
    exits and its pipes close. Before any thread starts, as fork needs."""
    if os.fork() != 0:
        os._exit(0)
    os.setsid()
    log.parent.mkdir(parents=True, exist_ok=True)
    out = os.open(log, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o644)
    null = os.open(os.devnull, os.O_RDONLY)
    os.dup2(null, 0)
    os.dup2(out, 1)
    os.dup2(out, 2)
    os.close(null)
    os.close(out)


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if "--" not in argv:
        parser().error("pytest's arguments follow --")
    split = argv.index("--")
    args = parser().parse_args(argv[:split])
    server = _bind(args.socket)
    _detach(args.log)
    recorder = Recorder()
    outbox, inbox = anyio.create_memory_object_stream[Request]()
    worker = Worker(outbox)
    with start_blocking_portal() as portal:
        serving, _ = portal.start_task(_serve, server, worker)
        loop = Loop(portal, inbox, recorder)
        with measured(args):
            # Not the host plugin here: these tests are already where they run.
            code = int(
                pytest.main(
                    [*argv[split + 1 :], "-p", "no:vivarium", "-p", "no:cacheprovider"], plugins=[recorder, loop]
                )
            )
        if worker.accepting is not None:
            portal.call(worker.accepting.cancel)
        if loop.stop is None:
            # pytest ended before its loop, as on a usage error: the host's call finds the connection closed.
            serving.cancel()
            return code
        loop.answer(loop.stop, code)
        serving.result(timeout=STOP_WAIT)
    return code


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""Drive `vivarium-mcp` the way Claude Code does: JSON-RPC over its stdio.

Raw, not the SDK's client: the SDK validates each incoming notification
against the methods it knows, and `notifications/claude/channel` is a
Claude Code extension. What this reads is what Claude Code reads.

    mcp_driver.py <vivarium-mcp> <spec of a run that fails a phase>
"""

from __future__ import annotations

import contextlib
import json
import os
import select
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

import anyio
from anyio.abc import Process, TaskGroup
from anyio.streams.buffered import BufferedByteReceiveStream

CHANNEL = "notifications/claude/channel"


def _object(value: object) -> dict[str, Any]:
    """A JSON value that must be an object; anything else is a broken server."""
    if not isinstance(value, dict):
        raise RuntimeError(f"vivarium-mcp sent {value!r}, not a JSON object")
    return value


LINE = 1 << 24
"""The longest JSON-RPC line read: far above any reply the server sends."""


class Client:
    def __init__(self, process: Process) -> None:
        if process.stdin is None or process.stdout is None:
            raise RuntimeError("vivarium-mcp started without pipes")
        self.stdin = process.stdin
        self.stdout = BufferedByteReceiveStream(process.stdout)
        self.next_id = 0
        self.channel: list[dict[str, Any]] = []

    async def send(self, message: dict[str, Any]) -> None:
        await self.stdin.send(json.dumps(message).encode() + b"\n")

    async def receive(self) -> dict[str, Any]:
        try:
            line = await self.stdout.receive_until(b"\n", LINE)
        except (anyio.EndOfStream, anyio.IncompleteRead):
            raise RuntimeError("vivarium-mcp closed its stdout") from None
        message = _object(json.loads(line))
        if message.get("method") == CHANNEL:
            self.channel.append(message["params"])
            print(f"[mcp] channel: {json.dumps(message['params'])[:300]}", flush=True)
        return message

    async def call(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        self.next_id += 1
        await self.send({"jsonrpc": "2.0", "id": self.next_id, "method": method, "params": params})
        while True:
            message = await self.receive()
            if message.get("id") == self.next_id:
                if "error" in message:
                    raise RuntimeError(f"{method}: {message['error']}")
                return _object(message["result"])

    async def tool(self, name: str, **arguments: Any) -> Any:
        result = await self.call("tools/call", {"name": name, "arguments": arguments})
        if result.get("isError"):
            raise RuntimeError(f"{name}: {result['content']}")
        return result["structuredContent"]

    async def until(self, event: str) -> dict[str, Any]:
        while True:
            for params in self.channel:
                if params["meta"].get("event") == event:
                    return params
            await self.receive()


def fail(text: str) -> None:
    print(f"FAIL: {text}", file=sys.stderr, flush=True)
    raise SystemExit(1)


class Shell:
    """A shell command, as Claude Code's Monitor runs it, read to its end in the background."""

    def __init__(self, group: TaskGroup, command: str) -> None:
        self.done = anyio.Event()
        self.result: subprocess.CompletedProcess[bytes] | None = None
        group.start_soon(self._run, command)

    async def _run(self, command: str) -> None:
        self.result = await anyio.run_process(command, check=False, stderr=None)
        self.done.set()

    async def wait(self, seconds: float) -> subprocess.CompletedProcess[bytes]:
        with anyio.fail_after(seconds):
            await self.done.wait()
        if self.result is None:
            raise RuntimeError("the command ended with no result")
        return self.result


async def run_shell(command: str, seconds: float) -> subprocess.CompletedProcess[bytes]:
    with anyio.fail_after(seconds):
        return await anyio.run_process(command, check=False, stderr=None)


async def main(server: str, spec: str) -> None:
    async with await anyio.open_process([server], stderr=None) as process, anyio.create_task_group() as group:
        await drive(Client(process), process, group, server, spec)
    await killed_server(server, spec)


def descendants(root: int) -> set[int]:
    """*root* and every process under it, by parent pid."""
    children: dict[int, list[int]] = {}
    for stat in Path("/proc").glob("[0-9]*/stat"):
        try:
            fields = stat.read_text().rpartition(")")[2].split()
        except OSError:
            continue
        children.setdefault(int(fields[1]), []).append(int(stat.parent.name))
    found: set[int] = set()
    todo = [root]
    while todo:
        pid = todo.pop()
        found.add(pid)
        todo.extend(children.get(pid, []))
    return found


async def killed_server(server: str, spec: str) -> None:
    """SIGKILL the server while its run holds a pause: the run and every
    process under it must exit, because nobody can reach that pause."""
    async with await anyio.open_process([server], stderr=None) as process:
        client = Client(process)
        await handshake(client)
        started = await client.tool("start", spec=spec)
        with anyio.fail_after(600):
            await client.until("paused")
        run = json.loads((Path(started["out"]) / "run.json").read_text())["pid"]
        watches: dict[int, int] = {}
        for pid in descendants(run):
            with contextlib.suppress(ProcessLookupError):
                watches[pid] = os.pidfd_open(pid)
        process.kill()
        await process.wait()
        try:
            with anyio.move_on_after(180) as late:
                for watch in watches.values():
                    await anyio.wait_readable(watch)
            if late.cancelled_caught:
                left = [pid for pid, watch in watches.items() if not select.select([watch], [], [], 0)[0]]
                fail(f"left after the server died: {sorted(left)}")
        finally:
            for watch in watches.values():
                os.close(watch)
    print(f"ok: the paused run and its {len(watches) - 1} processes exited with the server", flush=True)


async def handshake(client: Client) -> None:
    init = await client.call(
        "initialize",
        {
            "protocolVersion": "2025-06-18",
            "capabilities": {},
            "clientInfo": {"name": "mcp-driver", "version": "0"},
        },
    )
    if "claude/channel" not in init["capabilities"].get("experimental", {}):
        fail(f"no claude/channel capability: {init['capabilities']}")
    await client.send({"jsonrpc": "2.0", "method": "notifications/initialized"})


async def attached_elsewhere(server: str, out: Path) -> None:
    """A second server reaches the run by its `--out`, as it reaches a
    `vivarium up` environment, then exits without stopping it."""
    async with await anyio.open_process([server], stderr=None) as process:
        other = Client(process)
        await handshake(other)
        listed = (await other.tool("runs"))["runs"]
        if str(out.resolve()) not in {found["out"] for found in listed}:
            runtime = os.environ.get("XDG_RUNTIME_DIR")
            folder = Path(runtime) / "vivarium" / "runs" if runtime else Path(f"/tmp/vivarium-{os.getuid()}/runs")  # noqa: S108 -- the registry's own fallback
            entries = {link.name: os.readlink(link) for link in folder.iterdir()} if folder.is_dir() else None
            fail(f"runs on another server does not list {out}: {listed}; {folder} holds {entries}")
        state = await other.tool("state", run=str(out))
        if not state.get("pause"):
            fail(f"state by --out on another server: {state}")
        reply = await other.tool("exec", run=str(out), code='(await one.succeed("hostname")).stdout')
        if "one" not in str(reply.get("result")):
            fail(f"exec by --out did not reach the guest: {reply}")
        await other.stdin.aclose()
        with anyio.fail_after(60):
            await process.wait()
    print(f"ok: another server listed the run, reached it by --out and paused {state['pause']}", flush=True)


async def drive(client: Client, process: Process, group: TaskGroup, server: str, spec: str) -> None:
    await handshake(client)
    print("ok: the server declares claude/channel", flush=True)

    started = await client.tool("start", spec=spec)
    run = started["run"]
    print(f"ok: started {run}", flush=True)
    # The channel's events without channels: the command `start` gave,
    # in a shell, as Claude Code's Monitor runs it.
    watcher = Shell(group, started["monitor_all"] + " --json")
    armed = Shell(group, started["monitor"])

    with anyio.fail_after(600):
        paused = await client.until("paused")
    if paused["meta"].get("run") != run:
        fail(f"the pause names another run: {paused}")
    print(f"ok: a channel event said it paused: {paused['meta']}", flush=True)
    if not any(p["meta"].get("event") == "failed" for p in client.channel):
        fail("no channel event for the failed phase")
    print("ok: and one said which phase failed", flush=True)

    # For a harness that wakes an agent only when a command exits.
    at_pause = await armed.wait(60)
    output = at_pause.stdout
    if at_pause.returncode != 4 or "paused" not in output.decode().splitlines()[-1]:
        fail(f"monitor exited {at_pause.returncode} at the pause with {output.decode()!r}")
    print(f"ok: monitor exited 4 at the pause: {output.decode().splitlines()[-1]}", flush=True)
    late = await run_shell(started["monitor"], 60)
    if late.returncode != 4:
        fail(f"monitor armed while paused exited {late.returncode}, not 4")
    print("ok: monitor armed while paused exited 4 at once", flush=True)

    reply = await client.tool("exec", run=run, code='(await one.succeed("hostname")).stdout')
    if "one" not in str(reply.get("result")):
        fail(f"exec did not reach the guest: {reply}")
    print(f"ok: exec reached the paused guest: {reply['result']}", flush=True)

    await attached_elsewhere(server, Path(started["out"]))
    if not (await client.tool("state", run=run)).get("pause"):
        fail("the run lost its pause when a server attached to it exited")
    print("ok: the run still holds its pause after that server exited", flush=True)

    cases = (await client.tool("events", run=run, kind="case"))["events"]
    failed = [c for c in cases if c["data"]["outcome"] == "failed"]
    if not failed:
        fail(f"events found no failed case: {cases}")
    print(f"ok: events found the failed case: {failed[0]['text']}", flush=True)

    # A test from a working tree, against the paused guest; then an edit
    # to it, sent again. The second answer must be the edit's.
    tree = Path(tempfile.mkdtemp()) / "by_hand"
    tree.mkdir()
    test = tree / "test_by_hand.py"
    test.write_text("async def test_host(one):\n    assert (await one.succeed('hostname')).stdout == 'one'\n")
    reply = await client.tool("run_pytest", run=run, path=str(tree))
    if not reply["ok"] or reply["result"] != "1 passed":
        fail(f"run_pytest did not pass against the guest: {reply}")
    test.write_text("async def test_host(one):\n    assert (await one.succeed('hostname')).stdout == 'edited'\n")
    reply = await client.tool("run_pytest", run=run, path=str(tree), args=["-k", "host"])
    if reply["ok"]:
        fail(f"run_pytest ran the old test after an edit: {reply}")
    print("ok: run_pytest ran a local test, then its edit, against the paused guest", flush=True)

    await client.tool("resume", run=run)
    # Armed on the reply, as an agent does: the pause it replays is closed,
    # so it runs on to the verdict.
    rearmed = Shell(group, started["monitor"])
    with anyio.fail_after(300):
        finished = await client.until("finished")
    if finished["meta"].get("passed") != "false":
        fail(f"the verdict is wrong: {finished}")
    print(f"ok: a channel event gave the verdict: {finished['content']}", flush=True)

    watched_run = await watcher.wait(60)
    watched = [json.loads(line) for line in watched_run.stdout.decode().splitlines()]
    heard = [{**p["meta"], "text": p["content"]} for p in client.channel if p["meta"].get("run") == run]
    if watched != heard:
        fail(f"the monitor and the channel disagree:\n{watched}\n{heard}")
    if watched_run.returncode != 1:
        fail(f"the monitor exited {watched_run.returncode} for a failed run, not 1")
    print(f"ok: the monitor printed the channel's {len(watched)} events and exited 1", flush=True)

    late = await run_shell(started["monitor_all"], 60)
    lines = late.stdout.decode().splitlines()
    if late.returncode != 1 or len(lines) != len(heard):
        fail(f"a monitor after the verdict exited {late.returncode} with {lines}")
    print(f"ok: a monitor after the verdict replayed it: {lines[-1]}", flush=True)

    after_resume = await rearmed.wait(60)
    if after_resume.returncode != 1:
        fail(f"monitor armed after resume exited {after_resume.returncode}, not 1 for the verdict")
    print("ok: monitor armed after resume ran on to the verdict", flush=True)

    # The one an agent runs: no progress, so it wakes only when it must.
    quiet = await run_shell(started["monitor"] + " --json", 60)
    kept = [json.loads(line) for line in quiet.stdout.decode().splitlines()]
    wanted = [event for event in heard if event.get("event") not in {"progress", "resumed"}]
    if quiet.returncode != 1 or kept != wanted or len(wanted) == len(heard):
        fail(f"the quiet monitor exited {quiet.returncode} with {kept}, not {wanted}")
    print(f"ok: the quiet monitor printed {len(kept)} of {len(heard)} events: no progress", flush=True)

    await client.stdin.aclose()
    with anyio.fail_after(120):
        await process.wait()
    print("ok: the server exited when its client went away", flush=True)


if __name__ == "__main__":
    anyio.run(main, sys.argv[1], sys.argv[2])

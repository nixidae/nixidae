#!/usr/bin/env python3
"""Drive `vivarium-mcp` the way Claude Code does: JSON-RPC over its stdio.

Raw, not the SDK's client: the SDK validates each incoming notification
against the methods it knows, and `notifications/claude/channel` is a
Claude Code extension. What this reads is what Claude Code reads.

    mcp_driver.py <vivarium-mcp> <spec of a run that fails a phase>
"""

import json
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
"""The longest JSON-RPC line read: an `events` reply has no size limit."""


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
        await drive(Client(process), process, group, spec)


async def drive(client: Client, process: Process, group: TaskGroup, spec: str) -> None:
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
    print("ok: the server declares claude/channel", flush=True)
    await client.send({"jsonrpc": "2.0", "method": "notifications/initialized"})

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

    reply = await client.tool("exec", run=run, code='await one.succeed("hostname")')
    if "one" not in str(reply.get("result")):
        fail(f"exec did not reach the guest: {reply}")
    print(f"ok: exec reached the paused guest: {reply['result']}", flush=True)

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
    test.write_text("async def test_host(one):\n    assert (await one.succeed('hostname')).strip() == 'one'\n")
    reply = await client.tool("run_pytest", run=run, path=str(tree))
    if not reply["ok"] or reply["result"] != "1 passed":
        fail(f"run_pytest did not pass against the guest: {reply}")
    test.write_text("async def test_host(one):\n    assert (await one.succeed('hostname')).strip() == 'edited'\n")
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

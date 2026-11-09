"""`vivarium monitor`: one line per event, and the verdict as the exit status."""

from __future__ import annotations

import contextlib
import json
import os
import sys
from pathlib import Path
from typing import Any

import anyio
import pytest
from vivarium.control import RUN_FILE, SOCKET
from vivarium.monitor import (
    BUILD,
    BUILD_LOG,
    LIVE,
    PAUSED,
    PLANNED,
    SUBSTITUTE,
    Arrival,
    BuildTally,
    Watch,
    channel_event,
    follow,
    follow_file,
    line,
    locate,
    paused_now,
    status,
    wait_for_run,
)
from vivarium.phases import STOPPED


class TestLine:
    def test_plain(self) -> None:
        event = {"run": "r1", "event": "failed", "phase": "check", "text": "phase check failed: boom"}
        assert line(event) == "r1 failed check: phase check failed: boom"

    def test_a_message_of_several_lines_stays_on_one(self) -> None:
        event = {"run": "r1", "event": "exited", "text": "run exited 1 without a verdict:\nTraceback\n  boom\n"}
        assert "\n" not in line(event)
        assert line(event) == "r1 exited: run exited 1 without a verdict: | Traceback | boom"

    def test_json_is_one_object_per_line(self) -> None:
        event = {"run": "r1", "event": "progress", "text": "a\nb"}
        assert json.loads(line(event, as_json=True)) == event
        assert "\n" not in line(event, as_json=True)


class TestStatus:
    @pytest.mark.parametrize(
        ("event", "code"),
        [
            ({"event": "finished", "passed": "true"}, 0),
            ({"event": "finished", "passed": "false"}, 1),
            ({"event": "exited"}, 2),
            ({"event": "finished", "passed": "false", "stopped": "true"}, STOPPED),
            ({"event": "paused"}, None),
            ({"event": "progress"}, None),
        ],
    )
    def test_the_verdict_is_the_exit_status(self, event: dict[str, Any], code: int | None) -> None:
        assert status(event) == code

    def test_a_stopped_run_reads_stopped_not_failed(self) -> None:
        verdict = {"kind": "run_finished", "data": {"passed": False, "stopped": "ctl stop", "states": {"x": "pending"}}}
        pushed = channel_event(verdict, "r")
        assert pushed is not None
        text, meta = pushed
        assert text == "run stopped (ctl stop): x pending"
        assert status(meta) == STOPPED


PAUSE = {"run": "r", "event": "paused", "text": "paused after boot failed"}
RESUMED = {"run": "r", "event": "resumed", "text": "resumed, paused after boot failed"}
FAILED = {"run": "r", "event": "finished", "passed": "false", "text": "run failed"}


@pytest.mark.parametrize(
    ("events", "paused"),
    [
        ([], False),
        ([PAUSE], True),
        ([PAUSE, RESUMED], False),
        ([PAUSE, RESUMED, PAUSE], True),
        ([PAUSE, {"event": "progress"}], True),
        ([PAUSE, FAILED], False),
    ],
)
def test_paused_now_is_the_last_pause_left_open(events: list[dict[str, Any]], paused: bool) -> None:
    assert paused_now(events) == paused


def test_locate_takes_a_directory_or_an_id(tmp_path: Path) -> None:
    assert locate(str(tmp_path)) == tmp_path / SOCKET
    assert locate("vivarium-x-abc").name == SOCKET
    assert locate("vivarium-x-abc").parent.name == "vivarium-x-abc"


def test_locate_takes_an_out_that_does_not_exist_yet(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    assert locate("out") == tmp_path / "out" / SOCKET


def test_locate_prefers_a_run_id_that_exists(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("tempfile.tempdir", str(tmp_path))
    (tmp_path / "vivarium-x-abc").mkdir()
    monkeypatch.chdir(tmp_path / "vivarium-x-abc")
    assert locate("vivarium-x-abc") == tmp_path / "vivarium-x-abc" / SOCKET


@pytest.mark.anyio
async def test_wait_for_run_returns_when_the_run_starts(tmp_path: Path) -> None:
    out = tmp_path / "out"

    async def start() -> None:
        await anyio.sleep(0.3)
        out.mkdir()
        (out / "events.jsonl").touch()

    async with anyio.create_task_group() as group:
        group.start_soon(start)
        with anyio.fail_after(5):
            assert await wait_for_run(out, timeout=10) is Arrival.UP


@pytest.mark.anyio
async def test_wait_for_run_gives_up_after_its_timeout(tmp_path: Path) -> None:
    with anyio.fail_after(5):
        assert await wait_for_run(tmp_path / "never", timeout=0.3) is Arrival.NEVER


async def _dead_pid() -> int:
    process = await anyio.open_process([sys.executable, "-c", ""])
    await process.wait()
    return process.pid


def _run_json(out: Path, pid: int) -> None:
    out.mkdir(exist_ok=True)
    (out / RUN_FILE).write_text(json.dumps({"pid": pid}))


@pytest.mark.anyio
async def test_a_run_that_dies_while_it_builds_is_gone_with_its_reason(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _run_json(tmp_path, await _dead_pid())
    with (tmp_path / BUILD_LOG).open("w") as log:
        log.write(
            json.dumps({"action": "start", "id": 1, "type": BUILD, "text": "building '/nix/store/x.drv'", "fields": ["/nix/store/x.drv"]})
            + "\n"
        )
        log.write(json.dumps({"action": "failed", "text": "error: builder for x failed"}) + "\n")
    with anyio.fail_after(5):
        assert await wait_for_run(tmp_path, timeout=10, printer=Watch()) is Arrival.GONE
    assert capsys.readouterr().out.splitlines() == [
        f"{tmp_path.name} building: building '/nix/store/x.drv' (0/1 built)",
        f"{tmp_path.name} exited: no run: error: builder for x failed",
    ]


@pytest.mark.anyio
async def test_a_live_builder_is_waited_for(tmp_path: Path) -> None:
    _run_json(tmp_path, os.getpid())
    with anyio.fail_after(5):
        assert await wait_for_run(tmp_path, timeout=0.5) is Arrival.NEVER


@pytest.mark.anyio
async def test_quiet_leaves_out_the_builds(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    _run_json(tmp_path, await _dead_pid())
    (tmp_path / BUILD_LOG).write_text(json.dumps({"action": "start", "type": BUILD, "text": "building x"}) + "\n")
    with anyio.fail_after(5):
        assert await wait_for_run(tmp_path, timeout=10, printer=Watch(quiet=True)) is Arrival.GONE
    assert capsys.readouterr().out.splitlines() == [f"{tmp_path.name} exited: run exited before it started"]


async def _serve(path: Path, events: list[dict[str, Any]]) -> None:
    """One client, these events, then close."""
    async with await anyio.create_unix_listener(str(path)) as listener, await listener.accept() as stream:
        # A client that exits at a pause leaves before the rest.
        with contextlib.suppress(anyio.BrokenResourceError):
            for event in events:
                await stream.send(json.dumps(event).encode() + b"\n")


@pytest.mark.anyio
async def test_follow_prints_each_event_and_exits_with_the_verdict(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    socket = tmp_path / SOCKET
    events = [
        {"run": "r", "event": "progress", "phase": "boot", "text": "phase boot started"},
        {"run": "r", "event": "finished", "passed": "false", "text": "run failed: boot failed"},
    ]
    async with anyio.create_task_group() as group:
        group.start_soon(_serve, socket, events)
        while not socket.exists():
            await anyio.sleep(0.01)
        with anyio.fail_after(5):
            code = await follow(socket)
    assert code == 1
    assert capsys.readouterr().out.splitlines() == [
        "r progress boot: phase boot started",
        "r finished: run failed: boot failed",
    ]


@pytest.mark.anyio
async def test_quiet_leaves_out_progress(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    socket = tmp_path / SOCKET
    events = [
        {"run": "r", "event": "progress", "phase": "boot", "text": "phase boot passed"},
        {"run": "r", "event": "paused", "text": "after boot failed"},
        {"run": "r", "event": "finished", "passed": "false", "text": "run failed"},
    ]
    async with anyio.create_task_group() as group:
        group.start_soon(_serve, socket, events)
        while not socket.exists():
            await anyio.sleep(0.01)
        with anyio.fail_after(5):
            code = await follow(socket, quiet=True)
    assert code == 1
    assert capsys.readouterr().out.splitlines() == [
        "r paused: after boot failed",
        "r finished: run failed",
    ]


@pytest.mark.anyio
async def test_a_stream_that_ends_before_the_verdict_is_3(tmp_path: Path) -> None:
    socket = tmp_path / SOCKET
    async with anyio.create_task_group() as group:
        group.start_soon(_serve, socket, [{"run": "r", "event": "paused", "text": "paused"}])
        while not socket.exists():
            await anyio.sleep(0.01)
        with anyio.fail_after(5):
            assert await follow(socket) == 3


async def _follow(tmp_path: Path, events: list[dict[str, Any]], **options: Any) -> int:
    socket = tmp_path / SOCKET
    code = 0
    async with anyio.create_task_group() as group:
        group.start_soon(_serve, socket, events)
        while not socket.exists():
            await anyio.sleep(0.01)
        with anyio.fail_after(5):
            code = await follow(socket, **options)
    return code


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("events", "until_pause", "code"),
    [
        # A live pause: only --through-pauses goes on.
        ([LIVE, PAUSE, RESUMED, FAILED], True, PAUSED),
        ([LIVE, PAUSE, RESUMED, FAILED], False, 1),
        # Armed while paused: the replayed pause is still open.
        ([PAUSE, LIVE], True, PAUSED),
        ([PAUSE, LIVE], False, 3),
        # Armed after a resume: the replayed pause is closed.
        ([PAUSE, RESUMED, LIVE, FAILED], True, 1),
        # The replay alone decides nothing until it ends.
        ([PAUSE, RESUMED], True, 3),
    ],
)
async def test_until_pause_exits_at_an_open_pause(
    tmp_path: Path, events: list[dict[str, Any]], until_pause: bool, code: int
) -> None:
    assert await _follow(tmp_path, events, until_pause=until_pause) == code


@pytest.mark.anyio
async def test_live_is_not_printed(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    await _follow(tmp_path, [PAUSE, LIVE], until_pause=True, quiet=True)
    assert capsys.readouterr().out.splitlines() == ["r paused: paused after boot failed"]


def _write(out: Path, *events: dict[str, Any]) -> None:
    with (out / "events.jsonl").open("a") as file:
        for event in events:
            file.write(json.dumps(event) + "\n")


STARTED = {"kind": "phase_started", "phase": "boot"}
NOTE_PAUSED = {"kind": "note", "data": {"reason": "after boot failed"}}
NOTE_RESUMED = {"kind": "note", "data": {"resumed": "after boot failed"}}
VERDICT = {"kind": "run_finished", "data": {"passed": False, "states": {"boot": "failed"}}}


class TestFollowFile:
    """A run that no vivarium-mcp started: the monitor reads events.jsonl."""

    @pytest.mark.anyio
    async def test_a_finished_run_replays_to_its_verdict(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        _write(tmp_path, STARTED, VERDICT)
        with anyio.fail_after(5):
            assert await follow_file(tmp_path, grace=0) == 1
        assert capsys.readouterr().out.splitlines() == [
            f"{tmp_path.name} progress boot: phase boot started",
            f"{tmp_path.name} finished: run failed: boot failed",
        ]

    @pytest.mark.anyio
    async def test_a_run_gone_without_a_verdict_is_2(self, tmp_path: Path) -> None:
        _write(tmp_path, STARTED)
        with anyio.fail_after(5):
            assert await follow_file(tmp_path, grace=0.3) == 2

    @pytest.mark.anyio
    async def test_it_waits_while_the_run_is_up(self, tmp_path: Path) -> None:
        (tmp_path / "control.sock").touch()
        _write(tmp_path, STARTED, NOTE_PAUSED)
        async with anyio.create_task_group() as group:
            codes: list[int] = []

            async def watch() -> None:
                codes.append(await follow_file(tmp_path, grace=0, until_pause=False))

            group.start_soon(watch)
            await anyio.sleep(0.8)
            # The pause alone ends nothing with --through-pauses.
            assert codes == []
            _write(tmp_path, NOTE_RESUMED, VERDICT)
            (tmp_path / "control.sock").unlink()
            with anyio.fail_after(5):
                while not codes:
                    await anyio.sleep(0.05)
        assert codes == [1]

    @pytest.mark.anyio
    async def test_it_exits_at_an_open_pause_by_default(self, tmp_path: Path) -> None:
        (tmp_path / "control.sock").touch()
        _write(tmp_path, STARTED, NOTE_PAUSED)
        with anyio.fail_after(5):
            assert await follow_file(tmp_path, grace=0) == PAUSED

    @pytest.mark.anyio
    async def test_until_pause_runs_on_past_a_closed_pause(self, tmp_path: Path) -> None:
        (tmp_path / "control.sock").touch()
        _write(tmp_path, STARTED, NOTE_PAUSED, NOTE_RESUMED)
        with anyio.move_on_after(0.8) as scope:
            await follow_file(tmp_path, grace=0, until_pause=True)
        assert scope.cancelled_caught


class TestBuildTally:
    def test_a_path_counts_once_however_often_it_is_named(self) -> None:
        tally = BuildTally()
        records: list[dict[str, Any]] = [
            {"action": PLANNED, "type": BUILD, "fields": ["/nix/store/a.drv", "/nix/store/b.drv"]},
            {"action": PLANNED, "type": SUBSTITUTE, "fields": ["/nix/store/s"]},
            # A second plan names `b` again: a later realise asked for it too.
            {"action": PLANNED, "type": BUILD, "fields": ["/nix/store/b.drv", "/nix/store/c.drv"]},
            {"action": "start", "id": 1, "type": BUILD, "text": "building a", "fields": ["/nix/store/a.drv"]},
            {"action": "start", "id": 2, "type": SUBSTITUTE, "fields": ["/nix/store/s", "https://cache"]},
            {"action": "stop", "id": 2},
            {"action": "stop", "id": 1},
            # `a` again, under another activity: still one build.
            {"action": "start", "id": 3, "type": BUILD, "text": "building a", "fields": ["/nix/store/a.drv"]},
            {"action": "stop", "id": 3},
            # Nothing planned it: an import from a derivation.
            {"action": "start", "id": 4, "type": BUILD, "text": "building ifd", "fields": ["/nix/store/ifd.drv"]},
        ]
        events = [event for record in records if (event := tally.take(record, "r")) is not None]
        assert tally.counts() == {"built": "1", "builds": "4", "running": "1", "downloaded": "1", "downloads": "1"}
        assert events[-1]["event"] == "building"
        assert events[-1]["text"] == "building ifd (1/4 built)"

    def test_a_stop_of_an_activity_never_started_is_nothing(self) -> None:
        assert BuildTally().take({"action": "stop", "id": 9}, "r") is None

    def test_builds_events_are_for_json_only(self, capsys: pytest.CaptureFixture[str]) -> None:
        event = {"run": "r", "event": "builds", "text": "planned", "built": "0", "builds": "2"}
        Watch().take(event)
        Watch(as_json=True).take(event)
        assert capsys.readouterr().out.splitlines() == [json.dumps(event, separators=(",", ":"))]

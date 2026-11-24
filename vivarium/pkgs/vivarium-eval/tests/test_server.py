from __future__ import annotations

import base64
import json
import os
import signal
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

import anyio
import pytest
from mcp.shared.message import SessionMessage
from mcp.types import ImageContent, JSONRPCNotification, TextContent
from vivarium import places
from vivarium.control import PARENT, SOCKET, Op, Origin, Reply, RunFile
from vivarium.events import Event, Kind, Level
from vivarium.monitor import CLIP, LIVE, channel_event, clip
from vivarium.phases import PhaseState, Verdict
from vivarium_eval import server as server_module
from vivarium_eval.cli import explain
from vivarium_eval.server import (
    Attached,
    EventLine,
    Runs,
    Started,
    _reply,
    ask,
    budgeted,
    build,
    kernel_flags,
    run_argv,
    select,
    verdict,
    why_it_exited,
)

if TYPE_CHECKING:
    pass


class TestAnEvaluationError:
    # What Nix printed for an `after` naming a phase that does not exist,
    # measured through `vivarium-mcp`: the point was the last of 60 lines.
    NIX = (
        "\x1b[31;1merror:\x1b[0m\n"
        "       … while calling the 'derivationStrict' builtin\n"
        "         at /nix/store/x/lib/customisation.nix:405:12:\n"
        "       \x1b[31;1merror:\x1b[0m vivarium: these phases are named in an `after`"
        " and do not exist: bot."
    )

    def test_the_point_comes_first_without_colour(self) -> None:
        text = explain(self.NIX)
        assert text.startswith("error: vivarium: these phases")
        assert "\x1b" not in text
        assert "customisation.nix" in text, "the trace is kept, after the point"

    def test_the_channel_shows_the_point_not_the_trace(self) -> None:
        output = "[vivarium] evaluating broken\n[vivarium] evaluation failed: " + explain(self.NIX) + "\n"
        assert "do not exist: bot" in why_it_exited(output)

    def test_any_other_death_shows_the_end(self) -> None:
        output = "\n".join(f"line {n}" for n in range(40))
        assert why_it_exited(output).splitlines()[-1] == "line 39"


def event(kind: Kind, text: str = "", **fields: Any) -> Event:  # Any: Event's own keyword arguments
    return Event(at=0.0, kind=kind, level=Level.INFO, text=text, **fields)


class TestChannelEvent:
    def test_a_pause(self) -> None:
        content, meta = channel_event(event(Kind.PAUSED, "paused", data={"reason": "after cases failed"}), "r1") or (
            "",
            {},
        )
        assert meta == {"run": "r1", "event": "paused", "reason": "after cases failed"}
        assert "guests are up" in content

    def test_a_resume(self) -> None:
        pushed = channel_event(event(Kind.RESUMED, "resumed", data={"reason": "after cases failed"}), "r1")
        assert pushed == (
            "resumed, paused after cases failed",
            {"run": "r1", "event": "resumed", "reason": "after cases failed"},
        )

    def test_a_failed_phase(self) -> None:
        pushed = channel_event(
            event(Kind.PHASE_FINISHED, phase="cases", data={"state": "failed", "error": "boom"}), "r1"
        )
        assert pushed == ("phase cases failed: boom", {"run": "r1", "event": "failed", "phase": "cases"})

    def test_the_verdict(self) -> None:
        pushed = channel_event(
            event(Kind.RUN_FINISHED, data={"passed": False, "states": {"boot": "passed", "cases": "failed"}}),
            "r1",
        )
        assert pushed == (
            "run failed: boot passed, cases failed",
            {"run": "r1", "event": "finished", "passed": "false"},
        )

    def test_progress_at_each_phase_boundary(self) -> None:
        assert channel_event(event(Kind.PHASE_STARTED, phase="deploy"), "r1") == (
            "phase deploy started",
            {"run": "r1", "event": "progress", "phase": "deploy", "state": "started"},
        )
        assert channel_event(
            event(Kind.PHASE_FINISHED, phase="deploy", seconds=12.4, data={"state": "passed"}), "r1"
        ) == (
            "phase deploy passed in 12s",
            {"run": "r1", "event": "progress", "phase": "deploy", "state": "passed"},
        )

    @pytest.mark.parametrize(
        "quiet",
        [
            event(Kind.JOURNAL, "Started"),
            event(Kind.PHASE_FINISHED, data={"state": "skipped"}),
            event(Kind.NOTE, "control: exec", data={"op": "exec"}),
        ],
    )
    def test_the_rest_stays_in_the_file(self, quiet: Event) -> None:
        assert channel_event(quiet, "r1") is None

    def test_every_meta_key_is_an_identifier(self) -> None:
        """Claude Code drops a key with anything but letters, digits and _."""
        for pushed in (
            channel_event(event(Kind.PAUSED, data={"reason": "x"}), "r"),
            channel_event(event(Kind.PHASE_FINISHED, phase="p", data={"state": "failed"}), "r"),
            channel_event(event(Kind.RUN_FINISHED, data={"passed": True}), "r"),
        ):
            assert pushed is not None
            assert all(key.isidentifier() for key in pushed[1])


LINES = [
    e.as_json()
    for e in [
        event(Kind.JOURNAL, "a", machine="cp", data={"unit": "kubelet.service"}),
        event(Kind.JOURNAL, "b", machine="cp", data={"unit": "etcd.service"}),
        event(Kind.JOURNAL, "c", machine="w1", data={"unit": "kubelet.service"}),
        event(Kind.CASE, "t.py::test_x", phase="cases", data={"outcome": "failed"}),
        event(Kind.RPC, "hostname", machine="cp", phase="cases", data={"case": "t.py::test_x"}),
    ]
] + ["not json"]


class TestSelect:
    def test_one_service_on_one_machine(self) -> None:
        found = select(LINES, kind="journal", machine="cp", unit="kubelet.service")
        assert [e["text"] for e in found] == ["a"]

    def test_by_case(self) -> None:
        assert [e["text"] for e in select(LINES, case="test_x")] == ["hostname"]

    def test_the_last_n(self) -> None:
        assert [e["text"] for e in select(LINES, kind="journal", limit=2)] == ["b", "c"]

    def test_contains(self) -> None:
        assert [e["kind"] for e in select(LINES, contains="t.py")] == ["case"]


class TestRunArgv:
    def test_by_attribute_through_uml_eval(self, tmp_path: Path) -> None:
        argv = run_argv(
            out=tmp_path,
            attr="pytest-phase",
            spec=None,
            file="/src",
            breaks=["cases"],
            break_on_failure=True,
            only=[],
            offline=True,
            pytest_args=["-k", "x"],
        )
        assert argv[0].endswith("/vivarium")
        assert argv[1:5] == ["run", "pytest-phase", "--file", "/src"]
        assert argv[-7:] == ["--break", "cases", "--break-on-failure", "--offline", "--", "-k", "x"]

    def test_hold_runs_an_environment(self, tmp_path: Path) -> None:
        argv = run_argv(
            out=tmp_path,
            attr="k8s",
            spec=None,
            file="/src",
            breaks=[],
            break_on_failure=True,
            only=[],
            offline=False,
            pytest_args=[],
            hold=True,
        )
        assert argv[-2:] == ["--break-on-failure", "--hold"]

    def test_by_spec_straight_to_uml(self, tmp_path: Path) -> None:
        argv = run_argv(
            out=tmp_path,
            attr=None,
            spec="/nix/store/x-spec.json",
            file=".",
            breaks=[],
            break_on_failure=False,
            only=["cases"],
            offline=False,
            pytest_args=[],
        )
        assert argv[:5] == [sys.executable, "-m", "vivarium.cli", "run", "--spec"]
        assert argv[-2:] == ["--only", "cases"]

    def test_by_spec_with_the_runner_it_names(self, tmp_path: Path) -> None:
        """A spec from a newer lib.nix, run with this package's `vivarium`,
        lost its `pythonPath` without a word. Measured on nixkube."""
        argv = run_argv(
            out=tmp_path,
            attr=None,
            spec="/nix/store/x-spec.json",
            file=".",
            breaks=[],
            break_on_failure=False,
            only=[],
            offline=False,
            pytest_args=[],
            runner=Path("/nix/store/y-vivarium"),
        )
        assert argv[:4] == ["/nix/store/y-vivarium/bin/vivarium-runner", "run", "--spec", "/nix/store/x-spec.json"]

    def test_a_kernel_from_a_working_tree(self, tmp_path: Path) -> None:
        argv = run_argv(
            out=tmp_path,
            attr="x",
            spec=None,
            file=".",
            breaks=[],
            break_on_failure=True,
            only=[],
            offline=False,
            pytest_args=["-k", "y"],
            kernel="/home/me/linux/linux",
        )
        # Before `--`: after it, the flag would go to pytest.
        assert argv[argv.index("--kernel") + 1] == "/home/me/linux/linux"
        assert argv.index("--kernel") < argv.index("--")

    def test_a_kernel_per_guest(self) -> None:
        assert kernel_flags({"db": "/k/linux", "web": "/w/linux"}) == [
            "--kernel",
            "db=/k/linux",
            "--kernel",
            "web=/w/linux",
        ]
        assert kernel_flags("linux") == ["--kernel", str(Path("linux").absolute())]
        assert kernel_flags(None) == []

    def _cpus_argv(
        self, out: Path, cpus: int | dict[str, int] | None, cpu_scale: float | dict[str, float] | None
    ) -> list[str]:
        return run_argv(
            out=out,
            attr="x",
            spec=None,
            file=".",
            breaks=[],
            break_on_failure=True,
            only=[],
            offline=False,
            pytest_args=["-k", "y"],
            cpus=cpus,
            cpu_scale=cpu_scale,
        )

    def test_cpus_reach_the_run_before_pytest(self, tmp_path: Path) -> None:
        fixed = self._cpus_argv(tmp_path, 4, None)
        assert fixed[fixed.index("--cpus") + 1] == "4"
        assert fixed.index("--cpus") < fixed.index("--")
        scaled = self._cpus_argv(tmp_path, None, 0.5)
        assert scaled[scaled.index("--cpu-scale") + 1] == "0.5"
        assert "--cpus" not in scaled

    def test_cpus_per_guest(self, tmp_path: Path) -> None:
        argv = self._cpus_argv(tmp_path, {"db": 8}, 0.5)
        assert argv[argv.index("--cpus") + 1] == "db=8"
        assert argv[argv.index("--cpu-scale") + 1] == "0.5"

    @pytest.mark.parametrize(("cpus", "scale"), [(4, 0.5), ({"db": 2}, {"db": 0.5})])
    def test_one_guest_takes_one_of_cpus_and_cpu_scale(
        self, cpus: int | dict[str, int], scale: float | dict[str, float], tmp_path: Path
    ) -> None:
        with pytest.raises(ValueError, match="not both"):
            self._cpus_argv(tmp_path, cpus, scale)

    @pytest.mark.parametrize(("attr", "spec"), [(None, None), ("a", "b")])
    def test_exactly_one_of_attr_and_spec(self, attr: str | None, spec: str | None, tmp_path: Path) -> None:
        with pytest.raises(ValueError):
            run_argv(
                out=tmp_path,
                attr=attr,
                spec=spec,
                file=".",
                breaks=[],
                break_on_failure=True,
                only=[],
                offline=False,
                pytest_args=[],
            )


@dataclass
class FakeProcess:
    """A child that is still running, for a `Run` the test does not start."""

    returncode: int | None = None

    def send_signal(self, sig: int) -> None:
        pass

    async def wait(self) -> int:
        return 0

    def kill(self) -> None:
        pass


def _run(out: Path, backlog: list[dict[str, str]] | None = None, returncode: int | None = None) -> Started:
    process = FakeProcess()
    process.returncode = returncode
    return Started(id="r", out=out, process=process, backlog=backlog or [])


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


class Pushed:
    """What `Runs.push` sent to the channel."""

    def __init__(self) -> None:
        self.send, self.receive = anyio.create_memory_object_stream[SessionMessage](100)

    def contents(self) -> list[str]:
        found: list[str] = []
        while True:
            try:
                message = self.receive.receive_nowait()
            except anyio.WouldBlock:
                return found
            notification = message.message.root
            assert isinstance(notification, JSONRPCNotification)
            assert notification.params is not None
            found.append(f"{notification.params['meta']['event']}: {notification.params['content']}")


async def _serve_watch(path: Path, events: list[dict[str, str]]) -> None:
    """A run's control socket that answers `watch` with these, then ends."""
    async with await anyio.create_unix_listener(str(path)) as listener, await listener.accept() as stream:
        await stream.receive()
        for event in events:
            await stream.send(json.dumps(event).encode() + b"\n")


@pytest.mark.anyio
async def test_the_channel_carries_what_the_run_watch_streams(tmp_path: Path) -> None:
    pushed = Pushed()
    run = _run(tmp_path)
    events = [
        {"run": "r", "event": "progress", "phase": "boot", "text": "phase boot started"},
        LIVE,
        {"run": "r", "event": "finished", "passed": "true", "text": "run passed: boot passed"},
    ]
    async with anyio.create_task_group() as group:
        group.start_soon(_serve_watch, places.prepare(tmp_path) / SOCKET, events)
        runs = Runs(group, pushed.send)
        runs.runs["r"] = run
        with anyio.fail_after(5):
            await runs._watch(run)
    assert pushed.contents() == ["progress: phase boot started", "finished: run passed: boot passed"]
    assert run.finished
    assert [event["event"] for event in run.backlog] == ["progress", "finished"]


@pytest.mark.anyio
async def test_a_run_gone_before_its_socket_is_exited(tmp_path: Path) -> None:
    pushed = Pushed()
    run = _run(tmp_path, returncode=1)
    (tmp_path / "terminal.log").write_text("[vivarium] evaluation failed: error: boom\n")
    async with anyio.create_task_group() as group:
        runs = Runs(group, pushed.send)
        runs.runs["r"] = run
        with anyio.fail_after(5):
            await runs._watch(run)
    [exited] = pushed.contents()
    assert exited.startswith("exited: run exited 1 without a verdict:")
    assert "boom" in exited


@pytest.mark.anyio
async def test_a_run_is_told_the_server_pid(tmp_path: Path) -> None:
    argv = [sys.executable, "-c", f"import os; print(os.environ[{PARENT!r}])"]
    pushed = Pushed()
    async with anyio.create_task_group() as group:
        runs = Runs(group, pushed.send)
        with anyio.fail_after(5):
            run = await runs.start(argv, tmp_path, {})
            await run.process.wait()
    assert (tmp_path / "terminal.log").read_text() == f"{os.getpid()}\n"


@pytest.mark.anyio
async def test_the_runs_stop_at_once(tmp_path: Path) -> None:
    asked: list[int] = []
    both = anyio.Event()

    class Interrupted(FakeProcess):
        """Exits once every run got its SIGINT: a stop that waits for one first never ends."""

        def send_signal(self, sig: int) -> None:
            asked.append(sig)
            if len(asked) == 2:
                both.set()

        async def wait(self) -> int:
            await both.wait()
            return 0

    pushed = Pushed()
    async with anyio.create_task_group() as group:
        runs = Runs(group, pushed.send)
        for name in ("a", "b"):
            runs.runs[name] = Started(id=name, out=tmp_path / name, process=Interrupted())
        # Someone else's run: the server leaves it up.
        runs.runs["theirs"] = Attached(id="theirs", out=tmp_path / "theirs")
        with anyio.fail_after(5):
            await runs.stop_all()
    assert asked == [signal.SIGINT, signal.SIGINT]


@pytest.mark.anyio
@pytest.mark.parametrize("ok", [True, False])
async def test_a_screenshot_comes_back_as_an_image(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, ok: bool) -> None:
    png = tmp_path / "000.png"
    png.write_bytes(b"\x89PNG\r\n\x1a\nrest")
    asked: list[str] = []

    async def request(socket: Path, op: Op, code: str, reload: tuple[Path, ...] = ()) -> Reply:
        asked.append(code)
        if ok:
            return Reply(ok=True, output=f"{png}\n")
        return Reply(ok=False, error="MachineError: [blind] has no screen")

    monkeypatch.setattr(server_module, "request", request)
    send, _ = anyio.create_memory_object_stream[SessionMessage]()
    async with anyio.create_task_group() as group:
        runs = Runs(group, send)
        runs.runs["r"] = _run(tmp_path)
        result = await build([runs]).call_tool("screenshot", {"run": "r", "machine": "desk"})
        group.cancel_scope.cancel()
    assert not isinstance(result, dict)
    blocks = list(result)
    assert asked == ["print(await vms['desk'].screenshot(None))"]
    if ok:
        assert [block.type for block in blocks] == ["image", "text"]
        image, text = blocks
        assert isinstance(image, ImageContent)
        assert isinstance(text, TextContent)
        assert base64.b64decode(image.data) == png.read_bytes()
        assert text.text == str(png)
    else:
        # The negative: no image, and the guest's reason as text.
        assert [block.type for block in blocks] == ["text"]
        only = blocks[0]
        assert isinstance(only, TextContent)
        assert "has no screen" in only.text


def _run_file(out: Path, *, socket: bool = True, origin: Origin | None = None) -> None:
    about = RunFile(
        pid=os.getpid(),
        out=out,
        vivarium=Path("/nix/store/x-vivarium"),
        socket=out / "control.sock" if socket else None,
        commands=(),
        origin=origin,
    )
    (out / "run.json").write_text(about.to_json())


VERDICT = Verdict.of({"boot": PhaseState.PASSED, "check": PhaseState.FAILED}, {"check": "boom"}, None)


@pytest.mark.anyio
async def test_a_run_by_its_out_is_attached_once(tmp_path: Path) -> None:
    _run_file(tmp_path, origin=Origin(Path("/bin/vivarium"), "checks.x", Path("/src")))
    (tmp_path / "phases.json").write_text(VERDICT.to_json())
    pushed = Pushed()
    async with anyio.create_task_group() as group:
        runs = Runs(group, pushed.send)
        found = await runs.get(str(tmp_path))
        again = await runs.get(str(tmp_path / "." / ""))
    assert found is again
    assert isinstance(found, Attached)
    # Nothing answers on its socket: the run has ended.
    assert (found.running, found.status) == (False, "ended")
    assert found.source is not None
    assert (found.source.attr, found.source.file) == ("checks.x", "/src")
    assert verdict(found) == {
        "run": str(tmp_path),
        "status": "ended",
        "passed": False,
        "state": {"boot": "passed", "check": "failed"},
        "errors": {"check": "boom"},
    }


@pytest.mark.anyio
async def test_a_run_started_here_is_found_by_its_out(tmp_path: Path) -> None:
    send, _ = anyio.create_memory_object_stream[SessionMessage]()
    async with anyio.create_task_group() as group:
        runs = Runs(group, send)
        runs.runs["r"] = run = _run(tmp_path)
        assert await runs.get(str(tmp_path)) is run


@pytest.mark.anyio
@pytest.mark.parametrize("name", ["nothing", "no-run-json"])
async def test_no_run_names_what_a_tool_takes(tmp_path: Path, name: str) -> None:
    (tmp_path / "no-run-json").mkdir()
    send, _ = anyio.create_memory_object_stream[SessionMessage]()
    async with anyio.create_task_group() as group:
        runs = Runs(group, send)
        with pytest.raises(ValueError, match="no run"):
            await runs.get(str(tmp_path / name))


@pytest.mark.anyio
async def test_an_attached_run_pushes_only_what_happens_after(tmp_path: Path) -> None:
    pushed = Pushed()
    run = Attached(id=str(tmp_path), out=tmp_path)
    events = [
        {"run": tmp_path.name, "event": "paused", "reason": "before check", "text": "paused before check"},
        LIVE,
        {"run": tmp_path.name, "event": "resumed", "reason": "before check", "text": "resumed"},
        {"run": tmp_path.name, "event": "finished", "passed": "true", "text": "run passed"},
    ]

    async def serve_then_end() -> None:
        await _serve_watch(places.prepare(tmp_path) / SOCKET, events)
        run.gone.set()

    async with anyio.create_task_group() as group:
        group.start_soon(serve_then_end)
        runs = Runs(group, pushed.send)
        runs.runs[run.id] = run
        with anyio.fail_after(5):
            await runs._watch(run)
    # The pause was over before the server attached: no channel event for it.
    assert pushed.contents() == ["resumed: resumed", "finished: run passed"]
    assert [event["event"] for event in run.backlog] == ["paused", "resumed", "finished"]
    assert {event["run"] for event in run.backlog} == {str(tmp_path)}


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("returncode", "socket", "said"),
    [
        (None, True, "still evaluating or building"),
        (None, False, "no control socket"),
        (1, True, "has exited 1"),
    ],
)
async def test_a_tool_says_why_the_run_cannot_answer(
    tmp_path: Path, returncode: int | None, socket: bool, said: str
) -> None:
    _run_file(tmp_path, socket=socket)
    reply = await ask(_run(tmp_path, returncode=returncode), Op.STATE)
    assert not reply.ok
    assert reply.error is not None
    assert said in reply.error


class TestBounds:
    def test_short_text_is_kept(self) -> None:
        assert clip("x" * CLIP, "f") == "x" * CLIP

    def test_long_text_keeps_its_ends_and_names_the_file(self) -> None:
        text = "head" + "x" * 10_000 + "tail"
        short = clip(text, "/o/log")
        assert short.startswith("head")
        assert short.endswith("tail")
        assert f"[{len(text) - CLIP} characters elided; the whole text is in /o/log]" in short
        assert len(short) < CLIP + 100

    def test_a_failed_phase_is_one_bounded_channel_event(self) -> None:
        pushed = channel_event(
            event(Kind.PHASE_FINISHED, "", phase="check", data={"state": "failed", "error": "e" * 50_000}), "r"
        )
        assert pushed is not None
        assert len(pushed[0]) < CLIP + 200
        assert "whole text is in events.jsonl" in pushed[0]

    def test_a_long_reply_is_written_whole(self, tmp_path: Path) -> None:
        run = _run(tmp_path)
        long = "o" * 10_000
        short = _reply(Reply(ok=True, output=long, errors={"check": long}), run)
        [kept] = (tmp_path / "mcp").iterdir()
        assert json.loads(kept.read_text()) == {"ok": True, "output": long, "errors": {"check": long}, "refused": False}
        assert str(kept) in short["output"]
        assert str(kept) in short["errors"]["check"]

    def test_a_short_reply_writes_nothing(self, tmp_path: Path) -> None:
        assert _reply(Reply(ok=True, output="hi"), _run(tmp_path)) == {"ok": True, "output": "hi", "refused": False}
        assert not (tmp_path / "mcp").exists()

    def test_events_keep_the_newest_that_fit(self) -> None:
        events: list[EventLine] = [{"kind": "case", "text": f"{n}" + "c" * 1000} for n in range(100)]
        kept, dropped = budgeted(events, budget=10_000)
        assert kept[-1] == events[-1]
        assert dropped == 100 - len(kept)
        assert 5 < len(kept) < 10

    def test_one_huge_event_is_clipped_not_dropped(self) -> None:
        kept, dropped = budgeted([{"kind": "error", "text": "t" * 100_000}], budget=1000)
        assert dropped == 0
        assert "whole text is in events.jsonl" in kept[0]["text"]

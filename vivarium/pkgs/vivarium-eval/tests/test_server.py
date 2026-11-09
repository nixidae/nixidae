from __future__ import annotations

import base64
import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

import anyio
import pytest
from mcp.shared.message import SessionMessage
from mcp.types import ImageContent, JSONRPCNotification, TextContent
from vivarium.control import Op, Reply
from vivarium.monitor import LIVE, channel_event
from vivarium_eval import server as server_module
from vivarium_eval.cli import explain
from vivarium_eval.server import Run, Runs, build, run_argv, select, why_it_exited

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


def event(kind: str, **fields: object) -> dict[str, Any]:
    return {"kind": kind, **fields}


class TestChannelEvent:
    def test_a_pause(self) -> None:
        content, meta = channel_event(event("note", text="paused", data={"reason": "after cases failed"}), "r1") or (
            "",
            {},
        )
        assert meta == {"run": "r1", "event": "paused", "reason": "after cases failed"}
        assert "guests are up" in content

    def test_a_resume(self) -> None:
        pushed = channel_event(event("note", text="resumed", data={"resumed": "after cases failed"}), "r1")
        assert pushed == (
            "resumed, paused after cases failed",
            {"run": "r1", "event": "resumed", "reason": "after cases failed"},
        )

    def test_a_failed_phase(self) -> None:
        pushed = channel_event(event("phase_finished", phase="cases", data={"state": "failed", "error": "boom"}), "r1")
        assert pushed == ("phase cases failed: boom", {"run": "r1", "event": "failed", "phase": "cases"})

    def test_the_verdict(self) -> None:
        pushed = channel_event(
            event("run_finished", data={"passed": False, "states": {"boot": "passed", "cases": "failed"}}),
            "r1",
        )
        assert pushed == (
            "run failed: boot passed, cases failed",
            {"run": "r1", "event": "finished", "passed": "false"},
        )

    def test_progress_at_each_phase_boundary(self) -> None:
        assert channel_event(event("phase_started", phase="deploy"), "r1") == (
            "phase deploy started",
            {"run": "r1", "event": "progress", "phase": "deploy", "state": "started"},
        )
        assert channel_event(event("phase_finished", phase="deploy", seconds=12.4, data={"state": "passed"}), "r1") == (
            "phase deploy passed in 12s",
            {"run": "r1", "event": "progress", "phase": "deploy", "state": "passed"},
        )

    @pytest.mark.parametrize(
        "quiet",
        [
            event("journal", text="Started"),
            event("phase_finished", data={"state": "skipped"}),
            event("note", text="control: exec", data={"op": "exec"}),
        ],
    )
    def test_the_rest_stays_in_the_file(self, quiet: dict[str, Any]) -> None:
        assert channel_event(quiet, "r1") is None

    def test_every_meta_key_is_an_identifier(self) -> None:
        """Claude Code drops a key with anything but letters, digits and _."""
        for pushed in (
            channel_event(event("note", data={"reason": "x"}), "r"),
            channel_event(event("phase_finished", phase="p", data={"state": "failed"}), "r"),
            channel_event(event("run_finished", data={"passed": True}), "r"),
        ):
            assert pushed is not None
            assert all(key.isidentifier() for key in pushed[1])


LINES = [
    json.dumps(e)
    for e in [
        event("journal", machine="cp", text="a", data={"unit": "kubelet.service"}),
        event("journal", machine="cp", text="b", data={"unit": "etcd.service"}),
        event("journal", machine="w1", text="c", data={"unit": "kubelet.service"}),
        event("case", phase="cases", text="t.py::test_x", data={"outcome": "failed"}),
        event("rpc", machine="cp", phase="cases", text="hostname", data={"case": "t.py::test_x"}),
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

    def _cpus_argv(self, out: Path, cpus: int | None, cpu_scale: float | None) -> list[str]:
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

    def test_at_most_one_of_cpus_and_cpu_scale(self, tmp_path: Path) -> None:
        with pytest.raises(ValueError, match="at most one"):
            self._cpus_argv(tmp_path, 4, 0.5)

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


def _run(out: Path, backlog: list[dict[str, str]] | None = None, returncode: int | None = None) -> Run:
    process = FakeProcess()
    process.returncode = returncode
    return Run(id="r", out=out, process=process, backlog=backlog or [])


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
        group.start_soon(_serve_watch, tmp_path / "control.sock", events)
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
@pytest.mark.parametrize("ok", [True, False])
async def test_a_screenshot_comes_back_as_an_image(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, ok: bool) -> None:
    png = tmp_path / "000.png"
    png.write_bytes(b"\x89PNG\r\n\x1a\nrest")
    asked: list[str] = []

    async def request(socket: Path, op: Op, code: str) -> Reply:
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

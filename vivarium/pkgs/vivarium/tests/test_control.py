"""Breakpoints and injected Python, without a guest."""

from __future__ import annotations

import ast
import json
import shlex
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any, cast

import pytest
from vivarium.control import (
    Console,
    Controller,
    Op,
    Origin,
    Reply,
    Request,
    RunFile,
    Subject,
    Use,
    commands,
    forget,
    parse_request,
    provided,
    run_file,
    split_last_expression,
)
from vivarium.phases import PhaseState
from vivarium.session import Session


class TestSplitLastExpression:
    def test_a_trailing_expression_is_the_value(self) -> None:
        body, last = split_last_expression("x = 1\nx + 1")
        assert len(body.body) == 1
        assert last is not None
        assert isinstance(last.body, ast.BinOp)

    def test_a_trailing_statement_has_no_value(self) -> None:
        _, last = split_last_expression("x = 1")
        assert last is None

    def test_empty(self) -> None:
        body, last = split_last_expression("")
        assert body.body == []
        assert last is None


class TestParseRequest:
    def test_stream_round_trips_for_the_ops_that_print(self) -> None:
        assert parse_request(Request(Op.EXEC, "1", stream=True).encode()).stream
        assert not parse_request(Request(Op.EXEC, "1").encode()).stream

    def test_stream_is_dropped_for_an_op_that_prints_nothing(self) -> None:
        assert not parse_request(b'{"op": "state", "stream": true}').stream

    def test_a_request(self) -> None:
        assert parse_request(b'{"op": "exec", "arg": "1"}') == Request(Op.EXEC, "1")

    def test_state_needs_no_arg(self) -> None:
        assert parse_request(b'{"op": "state"}') == Request(Op.STATE)

    def test_a_request_reads_back_what_it_wrote(self) -> None:
        asked = Request(Op.INJECT, "/t/x.py", (Path("/t/lib"),))
        assert parse_request(asked.encode()) == asked

    @pytest.mark.parametrize(
        ("line", "says"),
        [
            (b"nope", "not JSON"),
            (b"[1]", "JSON object"),
            (b'{"op": "rm"}', "no such op"),
            (b'{"op": "exec"}', "needs an arg"),
            (b'{"op": "exec", "arg": 1}', "is a string"),
            (b'{"op": "inject", "arg": "x", "reload": ["lib"]}', "absolute"),
            (b'{"op": "exec", "arg": "1", "reload": ["/lib"]}', "reload is for"),
        ],
    )
    def test_what_is_refused(self, line: bytes, says: str) -> None:
        with pytest.raises(ValueError, match=says):
            parse_request(line)


@pytest.mark.anyio
class TestConsole:
    async def test_top_level_await_and_its_value(self) -> None:
        async def hostname() -> str:
            return "one"

        console = Console({"hostname": hostname})
        reply = await console.execute("await hostname()")
        assert reply.ok
        assert reply.result == "'one'"

    async def test_a_name_survives_to_the_next_call(self) -> None:
        console = Console({})
        await console.execute("pid = 42")
        reply = await console.execute("pid + 1")
        assert reply.result == "43"

    async def test_print_is_the_output(self) -> None:
        reply = await Console({}).execute("print('hello')")
        assert reply.output == "hello\n"
        assert reply.result is None

    async def test_an_exception_is_the_reply_not_a_crash(self) -> None:
        reply = await Console({}).execute("1 / 0")
        assert not reply.ok
        assert reply.error is not None
        assert "ZeroDivisionError" in reply.error

    async def test_a_syntax_error_is_the_reply(self) -> None:
        reply = await Console({}).execute("def (")
        assert not reply.ok
        assert "SyntaxError" in (reply.error or "")


VIVARIUM = Path("/nix/store/x-vivarium/bin/vivarium-runner")


@pytest.mark.anyio
async def test_state_names_each_failed_phase_and_why(tmp_path: Path) -> None:
    def emit(*_: Any, **__: Any) -> None:
        return None

    session = SimpleNamespace(
        out=tmp_path,
        state={"cluster": PhaseState.FAILED, "check": PhaseState.SKIPPED},
        errors={"cluster": "MachineError: VFS: Mount too revealing"},
        emit=emit,
        vms=None,
    )
    reply = await Controller(cast("Session", session)).handle(b'{"op": "state"}')
    assert reply.state == {"cluster": "failed", "check": "skipped"}
    assert reply.errors == {"cluster": "MachineError: VFS: Mount too revealing"}
    assert (reply.result, reply.pause) == ("running", None)


def test_a_directory_provides_its_modules_and_packages(tmp_path: Path) -> None:
    (tmp_path / "lab.py").write_text("")
    (tmp_path / "helpers").mkdir()
    (tmp_path / "helpers" / "__init__.py").write_text("")
    (tmp_path / "notes.txt").write_text("")
    assert provided(tmp_path) == {"lab", "helpers"}


def test_forget_drops_a_module_and_its_submodules_only() -> None:
    modules = {name: ModuleType(name) for name in ("lab", "helpers", "helpers.net", "helpersx", "os")}
    assert forget({"lab", "helpers"}, modules) == ["helpers", "helpers.net", "lab"]
    assert sorted(modules) == ["helpersx", "os"]


class TestReply:
    def test_a_reply_reads_back_what_it_wrote(self) -> None:
        reply = Reply(ok=True, result="paused", state={"x": "failed"}, pause="before y", errors={"x": "boom"})
        assert Reply.decode(reply.encode()) == reply

    def test_a_field_from_a_newer_runner_is_dropped(self) -> None:
        assert Reply.decode(b'{"ok": true, "result": "running", "later": 1}') == Reply(ok=True, result="running")

    def test_a_reply_from_an_older_runner_has_the_defaults(self) -> None:
        assert Reply.decode(b'{"ok": true}') == Reply(ok=True)


def test_the_run_file_reads_back_what_it_wrote() -> None:
    out = Path("/tmp/a run")
    about = run_file(VIVARIUM, out, 42, control=True)
    assert about.socket == out / "control.sock"
    assert list(about.commands) == commands(VIVARIUM, out)
    assert RunFile.from_json(about.to_json()) == about


def test_without_a_socket_the_run_file_names_only_the_monitor() -> None:
    about = run_file(VIVARIUM, Path("/o"), 42, control=False)
    assert about.socket is None
    assert [c.use for c in about.commands] == [Use.MONITOR]


def test_each_printed_command_reaches_the_run_it_names() -> None:
    out = Path("/tmp/a run")
    listed = commands(VIVARIUM, out)
    assert all(c.argv[0] == str(VIVARIUM) and str(out) in c.argv for c in listed)
    assert all(shlex.split(c.line) == list(c.argv) for c in listed)
    watch = next(c for c in listed if c.use is Use.MONITOR)
    assert watch.argv[1:] == ("monitor", str(out), "--quiet")
    ctl = {c.use: c.argv[1:5] for c in listed if c.needs_socket}
    not_ctl = {Use.MONITOR, Use.SWITCH, Use.RELOAD}
    assert ctl == {use: ("ctl", "--out", str(out), str(use)) for use in Use if use not in not_ctl}


ORIGIN = Origin(Path("/nix/store/y-vivarium/bin/vivarium"), "checks.switch", Path("/src/default.nix"))


def test_switch_is_printed_only_for_a_run_with_an_origin() -> None:
    out = Path("/o")
    assert all(c.use is not Use.SWITCH for c in commands(VIVARIUM, out))
    switch = next(c for c in commands(VIVARIUM, out, ORIGIN, "one") if c.use is Use.SWITCH)
    # No attribute: `vivarium switch` reads the origin back from run.json.
    assert switch.argv == (
        "/nix/store/y-vivarium/bin/vivarium",
        "switch",
        "--out",
        "/o",
        "--node",
        "one",
        "--module",
        "./module.nix",
    )


def test_reload_is_printed_only_for_a_run_with_an_origin() -> None:
    out = Path("/o")
    assert all(c.use is not Use.RELOAD for c in commands(VIVARIUM, out))
    reload = next(c for c in commands(VIVARIUM, out, ORIGIN, "one") if c.use is Use.RELOAD)
    assert reload.argv == ("/nix/store/y-vivarium/bin/vivarium", "reload", "--out", "/o")


def test_a_run_the_cli_started_names_the_cli_in_every_line() -> None:
    listed = commands(VIVARIUM, Path("/o"), ORIGIN, "one")
    assert {c.argv[0] for c in listed} == {str(ORIGIN.cli)}


def test_the_run_file_keeps_its_origin() -> None:
    about = run_file(VIVARIUM, Path("/o"), 42, control=True, origin=ORIGIN, node="one")
    assert RunFile.from_json(about.to_json()) == about
    assert about.command(Use.SWITCH) is not None


def test_the_run_file_names_the_test() -> None:
    test = Subject("pytest-phase", ("one", "two"), ("boot", "cases"), Path("/nix/store/s-spec.json"))
    about = run_file(VIVARIUM, Path("/o"), 42, control=True, test=test)
    assert json.loads(about.to_json())["test"] == {
        "name": "pytest-phase",
        "guests": ["one", "two"],
        "phases": ["boot", "cases"],
        "spec": "/nix/store/s-spec.json",
    }
    assert RunFile.from_json(about.to_json()) == about


def test_a_run_file_from_an_older_runner_has_no_test() -> None:
    document = json.loads(run_file(VIVARIUM, Path("/o"), 42, control=True).to_json())
    del document["test"]
    assert RunFile.from_json(json.dumps(document)).test is None


@pytest.mark.parametrize("text", ["[]", '{"cli": "/v", "attr": "x"}', '{"cli": "v", "attr": "x", "file": "/f"}'])
def test_a_bad_origin_is_refused(text: str) -> None:
    with pytest.raises(ValueError):
        Origin.from_json(text)

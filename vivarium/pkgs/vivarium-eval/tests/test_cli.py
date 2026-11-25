from __future__ import annotations

from pathlib import Path

import pytest
from vivarium import places
from vivarium.control import RUN_FILE, Op, Origin, run_file
from vivarium.exits import Exit
from vivarium_eval import cli as cli_module
from vivarium_eval.cli import (
    child_args,
    entry,
    forget_last_run,
    main,
    name_environment,
    not_ready,
    origin_of,
    parse,
    parser,
    split_attr,
)


def test_a_paused_environment_points_at_ctl_state() -> None:
    assert "vivarium ctl --out /o state" in not_ready(4, Path("/o"))


@pytest.mark.parametrize("code", [1, 2, 3, 5])
def test_an_ended_environment_points_at_its_files(code: int) -> None:
    """Its socket is gone, so `ctl state` would fail (nixidae/nixidae#1336)."""
    said = not_ready(code, Path("/o"))
    assert "ctl" not in said
    assert "/o/run.log" in said
    assert "/o/phases.json" in said


def test_a_new_run_forgets_the_last_runs_events_and_verdict(tmp_path: Path) -> None:
    for name in ("events.jsonl", "phases.json", RUN_FILE, "state.json"):
        (tmp_path / name).write_text("{}")
    forget_last_run(tmp_path)
    assert sorted(p.name for p in tmp_path.iterdir()) == sorted([RUN_FILE, "state.json"])
    forget_last_run(tmp_path)


class TestParse:
    def test_the_rest_goes_to_uml_untouched(self) -> None:
        request = parse(["run", "lan.qemu", "--out", "o", "-v", "--", "-k", "x"])
        assert request.command == "run"
        assert request.attr == ["lan", "qemu"]
        assert request.file == Path(".")
        # `--` survives: `vivarium` needs it to find pytest's arguments.
        assert request.rest == ["--out", "o", "-v", "--", "-k", "x"]

    def test_file_is_ours(self) -> None:
        request = parse(["phases", "recipes", "--file", "/src", "--out", "o"])
        assert request.file == Path("/src")
        assert request.rest == ["--out", "o"]

    def test_until_pause_is_ours_and_out_is_read_but_passed_on(self) -> None:
        request = parse(["run", "x", "--until-pause", "--out", "/o", "-v"])
        assert request.until_pause
        assert request.out == Path("/o")
        assert request.rest == ["--out", "/o", "-v"]

    def test_phases_takes_no_until_pause(self) -> None:
        assert not parse(["phases", "x"]).until_pause

    def test_up_waits_like_until_pause(self) -> None:
        request = parse(["up", "k8s", "--out", "/o"])
        assert (request.command, request.until_pause, request.out) == ("up", True, Path("/o"))

    def test_up_starts_a_held_run_with_hold_before_dashdash(self) -> None:
        assert child_args(["up", "k8s", "--out", "/o", "--", "-k", "x"]) == [
            "run",
            "k8s",
            "--out",
            "/o",
            "--hold",
            "--",
            "-k",
            "x",
        ]

    def test_until_pause_starts_the_run_without_the_flag(self) -> None:
        assert child_args(["run", "x", "--until-pause", "--out", "/o"]) == ["run", "x", "--out", "/o"]

    def test_the_attribute_may_come_after_the_runners_options(self) -> None:
        request = parse(["run", "--out", "o", "lan"])
        assert request.attr == ["lan"]
        assert request.rest == ["--out", "o"]

    def test_an_option_value_like_the_attribute_stays_with_its_option(self) -> None:
        request = parse(["run", "lan", "--only", "lan", "-q", "--out", "o"])
        assert request.attr == ["lan"]
        assert request.rest == ["--only", "lan", "-q", "--out", "o"]

    def test_the_runner_reads_the_words_as_they_were_written(self) -> None:
        request = parse(["run", "x", "--out=o", "-vv", "--cpus", "2", "--cpus", "one=4", "--no-control"])
        assert request.out == Path("o").absolute()
        assert request.rest == ["--out", "o", "-v", "-v", "--cpus", "2", "--cpus", "one=4", "--no-control"]

    def test_an_option_this_cli_does_not_know_goes_on_to_the_runner(self) -> None:
        """The exec'd runner is the evaluated library's, which can be newer."""
        assert parse(["run", "x", "--out", "o", "--newer"]).rest == ["--out", "o", "--newer"]

    @pytest.mark.parametrize(
        "argv",
        [["run", "x"], ["run", "x", "--until-pause"], ["run", "x", "--out", "o", "--cpus", "many"]],
    )
    def test_a_bad_runner_option_fails_before_an_evaluation(
        self, argv: list[str], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def evaluated(*_: object) -> None:
            raise AssertionError("evaluated with a bad runner option")

        monkeypatch.setattr(cli_module, "resolve", evaluated)
        with pytest.raises(SystemExit) as exited:
            main(argv)
        assert exited.value.code == Exit.USAGE

    def test_up_without_out_runs_in_its_named_environment(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def evaluated(*_: object) -> None:
            raise AssertionError("--name needs no evaluation")

        monkeypatch.setattr(cli_module, "test_name", evaluated)
        argv = name_environment(["up", "x.qemu", "--name", "dev", "-v", "--", "-k", "t"])
        out = places.environment("dev")
        assert argv == ["up", "x.qemu", "--file", ".", "--out", str(out), "-v", "--", "-k", "t"]
        assert parse(argv).out == out

    def test_up_takes_the_tests_name(self, monkeypatch: pytest.MonkeyPatch) -> None:
        async def named(*_: object) -> str:
            return "k8s-lb"

        monkeypatch.setattr(cli_module, "test_name", named)
        assert name_environment(["up", "checks.k8s-lb"])[4:6] == ["--out", str(places.environment("k8s-lb"))]

    def test_up_refuses_name_with_out(self) -> None:
        with pytest.raises(SystemExit):
            name_environment(["up", "x", "--name", "a", "--out", "o"])

    def test_help_lists_the_runners_options(self, capsys: pytest.CaptureFixture[str]) -> None:
        for command in ("run", "up"):
            with pytest.raises(SystemExit):
                main([command, "--help"])
            shown = capsys.readouterr().out
            assert "--break-on-failure" in shown
            assert "--cpus" in shown

    def test_an_option_after_dashdash_is_pytests_not_ours(self) -> None:
        request = parse(["run", "x", "--out", "o", "--", "--file", "t.py"])
        assert request.file == Path(".")
        assert request.rest == ["--out", "o", "--", "--file", "t.py"]


class TestRunnerCommands:
    """`ctl` and `monitor` are the runner's, taken the same way."""

    def test_ctl_takes_every_op(self) -> None:
        for op in Op:
            args = parser().parse_args(["ctl", "--out", "/o", str(op)])
            assert (args.command, args.op, args.out) == ("ctl", str(op), Path("/o"))

    def test_monitor_takes_the_runner_options(self) -> None:
        args = parser().parse_args(["monitor", "/o", "--quiet", "--through-pauses", "--wait", "5"])
        assert (args.target, args.quiet, args.through_pauses, args.wait) == ("/o", True, True, 5.0)

    def test_ctl_reaches_the_runner_code(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        with pytest.raises(SystemExit) as exited:
            main(["ctl", "--out", str(tmp_path), "state"])
        assert exited.value.code is Exit.NO_RUN
        assert "no run is listening" in capsys.readouterr().err

    def test_parse_refuses_a_command_that_does_not_evaluate(self) -> None:
        with pytest.raises(SystemExit):
            parse(["ctl", "--out", "/o", "state"])


class TestSwitchOrigin:
    """`switch` with `--out` alone evaluates what the run was started from."""

    def test_the_origin_comes_from_run_json(self, tmp_path: Path) -> None:
        origin = Origin(Path("/nix/store/v/bin/vivarium"), "checks.switch", Path("/src"))
        about = run_file(
            Path("/nix/store/r/bin/vivarium-runner"), tmp_path, 42, control=True, origin=origin, node="one"
        )
        (tmp_path / RUN_FILE).write_text(about.to_json())
        assert origin_of(None, None, tmp_path) == origin

    def test_an_attribute_given_wins(self, tmp_path: Path) -> None:
        origin = origin_of("lan", Path("/src"), tmp_path)
        assert (origin.attr, origin.file) == ("lan", Path("/src"))

    def test_a_run_by_nix_run_needs_the_attribute(self, tmp_path: Path) -> None:
        (tmp_path / RUN_FILE).write_text(run_file(Path("/r"), tmp_path, 42, control=True).to_json())
        with pytest.raises(ValueError, match="name the attribute"):
            origin_of(None, None, tmp_path)

    def test_no_run_needs_the_attribute(self, tmp_path: Path) -> None:
        with pytest.raises(ValueError, match="no run"):
            origin_of(None, None, tmp_path)

    def test_file_without_an_attribute_is_refused(self, tmp_path: Path) -> None:
        with pytest.raises(ValueError, match="--file"):
            origin_of(None, Path("/src"), tmp_path)


class TestAttr:
    def test_a_dotted_path(self) -> None:
        assert split_attr("lan.qemu") == ["lan", "qemu"]

    @pytest.mark.parametrize("text", ["", ".lan", "lan.", "lan..qemu"])
    def test_an_empty_part_is_refused(self, text: str) -> None:
        with pytest.raises(ValueError):
            split_attr(text)


class TestEntry:
    def test_a_directory_means_its_default_nix(self, tmp_path: Path) -> None:
        assert entry(tmp_path) == tmp_path / "default.nix"

    def test_a_file_is_itself(self, tmp_path: Path) -> None:
        path = tmp_path / "x.nix"
        path.write_text("{}")
        assert entry(path) == path

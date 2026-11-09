from __future__ import annotations

from pathlib import Path

import pytest
from vivarium.control import RUN_FILE, Op, Origin, run_file
from vivarium_eval.cli import entry, main, origin_of, parse, parser, split_attr


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

    def test_an_option_after_dashdash_is_pytests_not_ours(self) -> None:
        request = parse(["run", "x", "--", "--file", "t.py"])
        assert request.file == Path(".")
        assert request.rest == ["--", "--file", "t.py"]


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
        assert exited.value.code == 1
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

from __future__ import annotations

from pathlib import Path

import pytest
from vivarium.control import Op
from vivarium_eval.cli import entry, main, parse, parser, split_attr


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

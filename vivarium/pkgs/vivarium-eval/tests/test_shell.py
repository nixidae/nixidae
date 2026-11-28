from __future__ import annotations

import json
import os
import subprocess
import sys
from typing import TYPE_CHECKING

import pytest
from vivarium import exports, places
from vivarium.control import RUN_FILE, RunFile
from vivarium_eval import cli, shell

if TYPE_CHECKING:
    from pathlib import Path

PROXY = "socks5h://127.0.0.9:41733"


def test_a_proxy_becomes_the_variables_programs_read(tmp_path: Path) -> None:
    found = shell.variables(
        {exports.PROXY: PROXY, "KUBECONFIG": "/k"},
        environment="lab",
        out=tmp_path,
        proxied=True,
        wrappers=tmp_path / "bin",
        path="/usr/bin",
    )
    assert found["ALL_PROXY"] == found["https_proxy"] == found["http_proxy"] == PROXY
    assert found["NO_PROXY"] == shell.NO_PROXY
    assert found["PATH"] == f"{tmp_path / 'bin'}:/usr/bin"
    assert found["KUBECONFIG"] == "/k"
    assert found[shell.ENVIRONMENT] == "lab"
    assert found[shell.OUT] == str(tmp_path)


@pytest.mark.parametrize("exported", [{}, {exports.PROXY: PROXY}])
def test_no_proxy_without_one_or_when_declined(tmp_path: Path, exported: dict[str, str]) -> None:
    found = shell.variables(
        exported, environment=None, out=tmp_path, proxied=not exported, wrappers=tmp_path, path="/usr/bin"
    )
    assert not set(found) & {*shell.PROXY_VARIABLES, *shell.NO_PROXY_VARIABLES, "PATH", shell.ENVIRONMENT}


def test_script_quotes_for_the_shell() -> None:
    script = shell.script({"A": "it's here", "B": "$HOME"})
    done = subprocess.run(["sh", "-c", f'{script}printf "%s|%s" "$A" "$B"'], capture_output=True, text=True, check=True)
    assert done.stdout == "it's here|$HOME"


def wrapped_env(tmp_path: Path, *argv: str) -> dict[str, str]:
    """`env` run through a wrapper, as a shell with the proxy would run it."""
    directory = shell.wrappers(tmp_path / "run", sys.executable)
    fake = tmp_path / "fake"
    fake.mkdir()
    # A stand-in for `nix`: prints what it was given.
    (fake / "nix").write_text(f"#!{sys.executable}\nimport json, os\nprint(json.dumps(dict(os.environ)))\n")
    (fake / "nix").chmod(0o755)
    environment = dict.fromkeys(shell.PROXY_VARIABLES, PROXY) | {
        "PATH": os.pathsep.join([str(directory), str(fake), os.environ["PATH"]]),
        "KEPT": "yes",
    }
    done = subprocess.run([str(directory / argv[0]), *argv[1:]], env=environment, capture_output=True, text=True)
    assert done.returncode == 0, done.stderr
    seen: dict[str, str] = json.loads(done.stdout)
    return seen


def test_a_nix_command_runs_without_the_proxy(tmp_path: Path) -> None:
    seen = wrapped_env(tmp_path, "nix")
    assert not set(seen) & set(shell.PROXY_VARIABLES)
    assert seen["KEPT"] == "yes"
    assert str(tmp_path / "run" / shell.BIN) not in seen["PATH"].split(os.pathsep)


def test_unproxy_runs_any_command_without_the_proxy(tmp_path: Path) -> None:
    seen = wrapped_env(tmp_path, shell.UNPROXY, "nix")
    assert not set(seen) & set(shell.PROXY_VARIABLES)


def test_unproxy_without_a_command_says_how(tmp_path: Path) -> None:
    directory = shell.wrappers(tmp_path / "run", sys.executable)
    done = subprocess.run([str(directory / shell.UNPROXY)], capture_output=True, text=True)
    assert done.returncode == 1
    assert "usage: unproxy" in done.stderr


def test_wrappers_under_a_shared_runtime_root(tmp_path: Path) -> None:
    """The runtime root is made as a parent of `runs/`, with the umask's mode."""
    root = tmp_path / "run"
    root.mkdir(mode=0o755)
    root.chmod(0o755)
    directory = shell.wrappers(root, sys.executable)
    assert directory.stat().st_mode & 0o777 == 0o700


def test_wrappers_are_written_again_alike(tmp_path: Path) -> None:
    first = shell.wrappers(tmp_path, sys.executable)
    before = sorted((each.name, each.is_symlink()) for each in first.iterdir())
    shell.wrappers(tmp_path, sys.executable)
    assert sorted((each.name, each.is_symlink()) for each in first.iterdir()) == before
    assert len(before) == len(shell.UNPROXIED) + 1


@pytest.mark.parametrize("command", ["env", "shell", "browser"])
def test_a_run_that_is_not_up_is_refused(tmp_path: Path, command: str, capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as stopped:
        cli.main([command, "--out", str(tmp_path)])
    assert stopped.value.code == 3
    assert f"vivarium {command}: no run in {tmp_path}" in capsys.readouterr().err


def test_no_target_names_the_environments_that_are_up() -> None:
    with pytest.raises(SystemExit) as stopped:
        cli.main(["browser"])
    assert str(stopped.value).endswith("name an environment or give --out, one of them; none is up")
    # A run file whose pid lives and whose socket is not there yet: a run still starting, so up.
    out = places.environment("lab")
    out.mkdir(parents=True)
    about = RunFile(pid=os.getpid(), out=out, vivarium=out / "vivarium", socket=None, commands=())
    (out / RUN_FILE).write_text(about.to_json())
    with pytest.raises(SystemExit) as stopped:
        cli.main(["shell"])
    assert str(stopped.value).endswith("; up: lab")

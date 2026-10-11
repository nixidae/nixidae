"""The `#!nix` renderer, reached through Salt's own loader."""

import json
import pathlib
import subprocess
import sys
from typing import Any

import pytest
import salt.loader
from salt.exceptions import SaltRenderError


def test_the_loader_finds_the_renderer(opts: dict[str, Any]) -> None:
    assert "nix" in salt.loader.render(opts, {})


def test_an_attribute_set_is_the_highstate(render: Any) -> None:
    got = render('#!nix\n{ hello."pkg.installed" = [ { name = "hello"; } ]; }')
    assert got == {"hello": {"pkg.installed": [{"name": "hello"}]}}


def test_a_function_gets_grains_pillar_and_sls(render: Any) -> None:
    got = render('#!nix\n{ grains, pillar, sls, ... }: '
                 '{ "${sls}-${grains.os}"."test.nop" = [ { role = pillar.role; } ]; }')
    assert got == {"demo-Leap": {"test.nop": [{"role": "web"}]}}


def test_a_function_without_named_arguments_is_refused(render: Any) -> None:
    with pytest.raises(SaltRenderError):
        render("#!nix\nx: x")


def test_a_nix_error_is_a_render_error(render: Any) -> None:
    with pytest.raises(SaltRenderError, match="boom"):
        render('#!nix\nthrow "boom"')


def test_a_list_keeps_the_order_it_was_written_in(render: Any) -> None:
    got = render('#!nix\n[ { zebra."test.nop" = [ ]; } { apple."test.nop" = [ ]; } ]')
    assert list(got) == ["zebra", "apple"]


def test_a_relative_path_names_from_the_sls_directory(render: Any, tmp_path: pathlib.Path) -> None:
    got = render('#!nix\n{ f."test.nop" = [ { path = toString ./x; } ]; }')
    assert got["f"]["test.nop"][0]["path"] == str(tmp_path / "srv" / "x")


def test_salt_call_renders_a_nix_sls(tmp_path: pathlib.Path) -> None:
    """The whole path a minion takes: config, file_roots, the shebang."""
    srv = tmp_path / "srv"
    srv.mkdir()
    (srv / "demo.sls").write_text(
        '#!nix\n{ grains, ... }: { hi."test.nop" = [ { os = grains.os; } ]; }\n')
    conf = tmp_path / "conf"
    conf.mkdir()
    (conf / "minion").write_text(json.dumps({
        "file_client": "local",
        "root_dir": str(tmp_path),
        "file_roots": {"base": [str(srv)]},
        "grains": {"os": "Leap"},
        "id": "minion1",
    }))
    done = subprocess.run(
        [sys.executable, "-c", "import salt.scripts; salt.scripts.salt_call()",
         "--local", "--config-dir", str(conf), "--out", "json", "--log-level", "quiet",
         "state.show_sls", "demo"],
        capture_output=True, text=True, timeout=300)
    assert done.returncode == 0, done.stderr[-3000:]
    got = json.loads(done.stdout)["local"]
    # Compiled: `test.nop` splits into the module and the function.
    assert "nop" in got["hi"]["test"]
    assert {"os": "Leap"} in got["hi"]["test"]
    assert got["hi"]["__sls__"] == "demo"

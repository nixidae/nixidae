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


def test_an_import_beside_the_sls_comes_from_the_fileserver(
        render_served: Any, srv: pathlib.Path) -> None:
    (srv / "app").mkdir()
    (srv / "app" / "lib.nix").write_text('{ pkg = "nginx"; }\n')
    (srv / "app" / "web.sls").write_text(
        '#!nix\nlet lib = import ./lib.nix; in\n'
        '{ web."pkg.installed" = [ { name = lib.pkg; } ]; }\n')
    got = render_served("app.web", "app/web.sls")
    assert got == {"web": {"pkg.installed": [{"name": "nginx"}]}}


def test_salt_calls_an_execution_function(render_served: Any, srv: pathlib.Path) -> None:
    (srv / "calls.sls").write_text(
        '#!nix\n{ salt, ... }:\n'
        '{ c."test.nop" = [ { echo = salt "test.echo" [ "hi" ]; }\n'
        '                   { kw = (salt "test.arg" { a = 1; }).kwargs.a; } ]; }\n')
    got = render_served("calls", "calls.sls")
    assert got == {"c": {"test.nop": [{"echo": "hi"}, {"kw": 1}]}}


def test_salt_names_a_missing_function(render_served: Any, srv: pathlib.Path) -> None:
    (srv / "missing.sls").write_text('#!nix\n{ salt, ... }: salt "no.such" [ ]\n')
    with pytest.raises(SaltRenderError, match=r"no execution function 'no\.such'"):
        render_served("missing", "missing.sls")


def test_an_init_sls_imports_from_its_own_directory(
        render_served: Any, srv: pathlib.Path) -> None:
    (srv / "svc").mkdir()
    (srv / "svc" / "port.nix").write_text("8080\n")
    (srv / "svc" / "init.sls").write_text(
        '#!nix\n{ svc."test.nop" = [ { port = import ./port.nix; } ]; }\n')
    assert render_served("svc", "svc/init.sls") == {"svc": {"test.nop": [{"port": 8080}]}}


def test_a_file_reads_as_its_bytes(render_served: Any, srv: pathlib.Path) -> None:
    (srv / "motd").write_bytes(b"caf\xc3\xa9\n")
    (srv / "top.sls").write_text(
        '#!nix\n{ m."test.nop" = [ { text = builtins.readFile ./motd; '
        'names = builtins.attrNames (builtins.readDir ./.); } ]; }\n')
    got = render_served("top", "top.sls")["m"]["test.nop"][0]
    assert got == {"text": "café\n", "names": ["motd", "top.sls"]}


def test_a_missing_import_is_a_render_error(render_served: Any, srv: pathlib.Path) -> None:
    (srv / "bad.sls").write_text('#!nix\nimport ./nothing.nix\n')
    with pytest.raises(SaltRenderError, match=r"nothing\.nix"):
        render_served("bad", "bad.sls")


def test_each_render_collects(render: Any) -> None:
    """The collector runs after a render, so a minion does not grow to
    Nix's initial heap before its first collection (nixidae/nixidae#752)."""
    import huggorm_bindings

    before = huggorm_bindings.gc_stats()["collections"]
    render('#!nix\n{ a."test.nop" = [ ]; }')
    assert huggorm_bindings.gc_stats()["collections"] > before


def _salt_call(conf: dict[str, Any], tmp_path: pathlib.Path, *args: str) -> Any:
    """`salt-call --local`, as a masterless minion runs it."""
    cfg = tmp_path / "conf"
    cfg.mkdir(exist_ok=True)
    (cfg / "minion").write_text(json.dumps({
        "file_client": "local", "root_dir": str(tmp_path), "id": "minion1", **conf}))
    done = subprocess.run(
        [sys.executable, "-c", "import salt.scripts; salt.scripts.salt_call()",
         "--local", "--config-dir", str(cfg), "--out", "json", "--log-level", "quiet", *args],
        capture_output=True, text=True, timeout=300)
    assert done.returncode == 0, done.stderr[-3000:]
    return json.loads(done.stdout)["local"]


def test_ext_pillar_gives_a_minion_its_pillar(tmp_path: pathlib.Path) -> None:
    nixdir = tmp_path / "nixpillar"
    nixdir.mkdir()
    (nixdir / "roles.nix").write_text('{ minion1 = "web"; }\n')
    (nixdir / "default.nix").write_text(
        "{ id, grains, pillar, ... }:\n"
        "{ role = (import ./roles.nix).${id}; os = grains.os; before = pillar.base or null; }\n")
    srv = tmp_path / "pillar"
    srv.mkdir()
    (srv / "top.sls").write_text("base:\n  '*':\n    - common\n")
    (srv / "common.sls").write_text("base: yaml\n")
    got = _salt_call({
        "pillar_roots": {"base": [str(srv)]},
        "grains": {"os": "Leap"},
        "ext_pillar": [{"nix": str(nixdir / "default.nix")}],
    }, tmp_path, "pillar.items", "unmask=True")
    assert got["role"] == "web"
    assert got["os"] == "Leap"
    assert got["before"] == "yaml"


def test_ext_pillar_takes_options(tmp_path: pathlib.Path) -> None:
    (tmp_path / "p.nix").write_text("{ fromOptions = true; }\n")
    got = _salt_call({"ext_pillar": [{"nix": {"file": str(tmp_path / "p.nix")}}]},
                     tmp_path, "pillar.items", "unmask=True")
    assert got["fromOptions"] is True


def test_a_nix_pillar_sls_imports_from_pillar_roots(tmp_path: pathlib.Path) -> None:
    """nixidae/nixidae#754: in a pillar render the file client serves pillar_roots."""
    srv = tmp_path / "pillar"
    srv.mkdir()
    (srv / "top.sls").write_text("base:\n  '*':\n    - db\n")
    (srv / "port.nix").write_text("5432\n")
    (srv / "db.sls").write_text("#!nix\n{ dbPort = import ./port.nix; }\n")
    got = _salt_call({"pillar_roots": {"base": [str(srv)]}}, tmp_path,
                     "pillar.items", "unmask=True")
    assert got.get("dbPort") == 5432, got.get("_errors")


def test_the_execution_module_evaluates_on_the_minion(tmp_path: pathlib.Path) -> None:
    got = _salt_call({"grains": {"os": "Leap"}}, tmp_path,
                     "havsorm.eval", "{ grains, ... }: [ grains.os (1 + 1) ]")
    assert got == ["Leap", 2]


def test_the_execution_module_reports_a_nix_error(tmp_path: pathlib.Path) -> None:
    cfg = tmp_path / "conf"
    cfg.mkdir()
    (cfg / "minion").write_text(json.dumps(
        {"file_client": "local", "root_dir": str(tmp_path), "id": "minion1"}))
    done = subprocess.run(
        [sys.executable, "-c", "import salt.scripts; salt.scripts.salt_call()",
         "--local", "--config-dir", str(cfg), "--out", "json", "--log-level", "quiet",
         "havsorm.eval", 'throw "nope"'],
        capture_output=True, text=True, timeout=300)
    assert done.returncode != 0
    assert "nope" in done.stdout + done.stderr


def test_a_pillar_sls_without_a_render_client_reads_pillar_roots(
        opts: dict[str, Any], tmp_path: pathlib.Path) -> None:
    """Salt 3006 gives a renderer no file client, and its cp functions
    serve file_roots. The pillar SLS's own path names its tree."""
    import io

    pillar = tmp_path / "pillar"
    pillar.mkdir()
    (pillar / "port.nix").write_text("5432\n")
    sls = pillar / "db.sls"
    sls.write_text("#!nix\n{ dbPort = import ./port.nix; }\n")
    opts["pillar_roots"] = {"base": [str(pillar)]}
    renderers = salt.loader.render(opts, salt.loader.minion_mods(opts), pillar={})
    got = renderers["nix"](io.StringIO(sls.read_text()), saltenv="base", sls="db",
                           tmplpath=str(sls))
    assert got == {"dbPort": 5432}

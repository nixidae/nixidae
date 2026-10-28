"""A `#!nix` reactor SLS, rendered by Salt's own reactor on a master."""

import pathlib
from typing import Any

import pytest
import salt.config
import salt.utils.reactor

from havsorm import nixeval


@pytest.fixture
def reactor(tmp_path: pathlib.Path) -> salt.utils.reactor.Reactor:
    opts: dict[str, Any] = salt.config.master_config(None)
    opts.update(
        root_dir=str(tmp_path),
        cachedir=str(tmp_path / "cache"),
        pki_dir=str(tmp_path / "pki"),
        sock_dir=str(tmp_path / "sock"),
        file_roots={"base": [str(tmp_path / "srv")]},
    )
    (tmp_path / "srv").mkdir()
    return salt.utils.reactor.Reactor(opts)


def test_a_reaction_reads_the_event(reactor: Any, tmp_path: pathlib.Path) -> None:
    reaction = tmp_path / "start.sls"
    reaction.write_text(
        "#!nix\n"
        "{ tag, data, ... }:\n"
        '{ "sync-${data.id}"."local.saltutil.sync_grains" = [\n'
        "    { tgt = data.id; } { args = [ { event = tag; } ]; } ]; }\n"
    )

    (chunk,) = reactor.reactions("salt/minion/web1/start", {"id": "web1"}, [str(reaction)])

    assert chunk["state"] == "local"
    assert chunk["fun"] == "saltutil.sync_grains"
    assert chunk["tgt"] == "web1"
    assert chunk["args"] == [{"event": "salt/minion/web1/start"}]


def test_the_reactor_evaluator_stays_warm_and_sees_an_edit(reactor: Any, tmp_path: pathlib.Path) -> None:
    srv = tmp_path / "srv"
    lib = srv / "lib.nix"
    reaction = srv / "deploy.sls"
    reaction.write_text(
        "#!nix\n"
        "{ data, salt, ... }:\n"
        '{ deploy."local.test.ping" = [\n'
        '    { tgt = salt "test.echo" [ (import ./lib.nix).target ]; } ]; }\n'
    )

    def target() -> str:
        (chunk,) = reactor.reactions("havsorm/deploy", {"id": "m"}, [str(reaction)])
        found: str = chunk["tgt"]
        return found

    lib.write_text('{ target = "one"; }\n')
    assert target() == "one"
    evaluator = nixeval.warm("dummy://")
    cached = [p for p in evaluator.state.cached_files() if p.endswith("/lib.nix")]
    assert cached, evaluator.state.cached_files()

    assert target() == "one"
    assert nixeval.warm("dummy://") is evaluator
    assert [p for p in evaluator.state.cached_files() if p.endswith("/lib.nix")] == cached

    lib.write_text('{ target = "two"; }\n')
    assert target() == "two"

    lib.write_text('{ target = "three" }\n')
    assert reactor.reactions("havsorm/deploy", {"id": "m"}, [str(reaction)]) == []
    lib.write_text('{ target = "three"; }\n')
    assert target() == "three"


def test_a_fork_makes_its_own_evaluator(monkeypatch: Any) -> None:
    parent = nixeval.warm("dummy://")
    monkeypatch.setattr(nixeval.os, "getpid", lambda: parent.pid + 1)
    child = nixeval.warm("dummy://")
    assert child is not parent
    assert child.pid == parent.pid + 1

from __future__ import annotations

import json
from pathlib import Path

import pytest
from vivarium import exports
from vivarium_runner import MachineError, Machines


@pytest.mark.anyio
async def test_a_later_value_replaces_one() -> None:
    sink = exports.Exports()
    await sink.set("KUBECONFIG", "/a")
    await sink.set("KUBECONFIG", "/b")
    assert sink.values == {"KUBECONFIG": "/b"}


@pytest.mark.anyio
async def test_a_bad_name_is_refused() -> None:
    sink = exports.Exports()
    with pytest.raises(exports.ExportError):
        await sink.set("A B", "x")
    assert sink.values == {}


@pytest.mark.anyio
async def test_a_run_with_no_sink_refuses() -> None:
    with pytest.raises(MachineError, match="exports nothing"):
        await Machines().export("A", "b")


def test_the_reply_round_trips() -> None:
    values = {"KUBECONFIG": "/k", exports.PROXY: "socks5h://127.0.0.1:1"}
    assert exports.decode(exports.encode(values)) == values


@pytest.mark.parametrize("content", [["A"], {"A": 1}, {"A B": "x"}])
def test_a_malformed_reply_is_refused(content: object) -> None:
    with pytest.raises(exports.ExportError):
        exports.decode(json.dumps(content))


def test_a_phase_gets_the_variables_vivarium_shell_sets(tmp_path: Path) -> None:
    proxy = "socks5h://127.0.0.1:1080"
    found = exports.shell({exports.PROXY: proxy, "KUBECONFIG": "/k"}, environment="lab", out=tmp_path)
    assert {found[name] for name in exports.PROXY_VARIABLES} == {proxy}
    assert found["no_proxy"] == exports.NO_PROXY
    assert (found["KUBECONFIG"], found[exports.ENVIRONMENT], found[exports.OUT]) == ("/k", "lab", str(tmp_path))
    assert not set(exports.shell({}, environment=None, out=tmp_path)) & set(exports.PROXY_VARIABLES)

"""The Nix store states, through `salt-call --local`, against a chroot store
that substitutes from a `file://` binary cache. No daemon, no network."""

import json
import os
import pathlib
import subprocess
import sys
from typing import Any

import pytest


@pytest.fixture
def cached(tmp_path: pathlib.Path) -> tuple[str, str]:
    """A path in a binary cache and in no other store: (cache URI, path)."""
    from huggorm_bindings import ContentAddressMethod, HashAlgorithm, Store

    source = Store(str(tmp_path / "source"))
    path = source.add_to_store("hello", b"hello\n", ContentAddressMethod.NAR,
                               HashAlgorithm.SHA256)
    cache = f"file://{tmp_path / 'cache'}?compression=none"
    source.copy_closure(Store(cache), [path], check_sigs=False)
    return cache, source.print_store_path(path)


def _state(tmp_path: pathlib.Path, cache: str, *args: str) -> Any:
    cfg = tmp_path / "conf"
    cfg.mkdir(exist_ok=True)
    (cfg / "minion").write_text(json.dumps({
        "file_client": "local", "root_dir": str(tmp_path / "salt"), "id": "minion1",
        "havsorm": {
            "store": str(tmp_path / "minion"),
            "nix_settings": {"substituters": cache, "require-sigs": "false"},
        },
    }))
    done = subprocess.run(
        [sys.executable, "-c", "import salt.scripts; salt.scripts.salt_call()",
         "--local", "--config-dir", str(cfg), "--out", "json", "--log-level", "quiet",
         "state.single", "havsorm.realised", *args],
        capture_output=True, text=True, timeout=300)
    assert done.stdout, done.stderr[-3000:]
    (ret,) = json.loads(done.stdout)["local"].values()
    return ret


def test_realised_substitutes_and_roots_once(
        tmp_path: pathlib.Path, cached: tuple[str, str]) -> None:
    cache, path = cached
    root = str(tmp_path / "roots" / "hello")
    os.makedirs(os.path.dirname(root))

    dry = _state(tmp_path, cache, path, f"root={root}", "test=True")
    assert dry["result"] is None, dry
    assert dry["changes"] == {"realised": path, "root": root}
    assert not os.path.lexists(root)

    done = _state(tmp_path, cache, path, f"root={root}")
    assert done["result"] is True, done
    assert done["changes"] == {"realised": path, "root": root}
    assert os.readlink(root) == path
    store = tmp_path / "minion" / path.lstrip("/")
    assert store.read_text() == "hello\n"

    again = _state(tmp_path, cache, path, f"root={root}")
    assert again["result"] is True, again
    assert again["changes"] == {}


def test_a_path_no_cache_has_fails_the_state(
        tmp_path: pathlib.Path, cached: tuple[str, str]) -> None:
    cache, _ = cached
    missing = "/nix/store/00000000000000000000000000000000-nothing"
    ret = _state(tmp_path, cache, missing)
    assert ret["result"] is False, ret
    assert ret["changes"] == {}

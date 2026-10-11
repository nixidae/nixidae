"""The Nix store states, through `salt-call --local`, against a chroot store
that substitutes from a `file://` binary cache. No daemon, no network."""

import json
import os
import pathlib
import subprocess
import sys
from typing import Any

import pytest


def _cache(tmp_path: pathlib.Path, *contents: bytes) -> tuple[str, list[str]]:
    """Paths in a binary cache and in no other store: (cache URI, paths)."""
    from huggorm_bindings import ContentAddressMethod, HashAlgorithm, Store

    source = Store(str(tmp_path / "source"))
    paths = [source.add_to_store(f"env-{i}", data, ContentAddressMethod.NAR,
                                 HashAlgorithm.SHA256) for i, data in enumerate(contents)]
    cache = f"file://{tmp_path / 'cache'}?compression=none"
    source.copy_closure(Store(cache), paths, check_sigs=False)
    return cache, [source.print_store_path(p) for p in paths]


@pytest.fixture
def cached(tmp_path: pathlib.Path) -> tuple[str, str]:
    """A path in a binary cache and in no other store: (cache URI, path)."""
    cache, (path,) = _cache(tmp_path, b"hello\n")
    return cache, path


def _state(tmp_path: pathlib.Path, cache: str, fun: str, *args: str) -> Any:
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
         "state.single", fun, *args],
        capture_output=True, text=True, timeout=300)
    assert done.stdout, done.stderr[-3000:]
    (ret,) = json.loads(done.stdout)["local"].values()
    return ret


def test_realised_substitutes_and_roots_once(
        tmp_path: pathlib.Path, cached: tuple[str, str]) -> None:
    cache, path = cached
    root = str(tmp_path / "roots" / "hello")
    os.makedirs(os.path.dirname(root))

    dry = _state(tmp_path, cache, "havsorm.realised", path, f"root={root}", "test=True")
    assert dry["result"] is None, dry
    assert dry["changes"] == {"realised": path, "root": root}
    assert not os.path.lexists(root)

    done = _state(tmp_path, cache, "havsorm.realised", path, f"root={root}")
    assert done["result"] is True, done
    assert done["changes"] == {"realised": path, "root": root}
    assert os.readlink(root) == path
    store = tmp_path / "minion" / path.lstrip("/")
    assert store.read_bytes() == b"hello\n"

    again = _state(tmp_path, cache, "havsorm.realised", path, f"root={root}")
    assert again["result"] is True, again
    assert again["changes"] == {}


def test_a_path_no_cache_has_fails_the_state(
        tmp_path: pathlib.Path, cached: tuple[str, str]) -> None:
    cache, _ = cached
    missing = "/nix/store/00000000000000000000000000000000-nothing"
    ret = _state(tmp_path, cache, "havsorm.realised", missing)
    assert ret["result"] is False, ret
    assert ret["changes"] == {}


def test_a_profile_switches_and_keeps_the_old_generation(tmp_path: pathlib.Path) -> None:
    cache, (one, two) = _cache(tmp_path, b"one", b"two")
    profile = str(tmp_path / "profiles" / "salt")
    os.makedirs(os.path.dirname(profile))

    dry = _state(tmp_path, cache, "havsorm.profile", profile, f"path={one}", "test=True")
    assert dry["result"] is None, dry
    assert dry["changes"] == {"old": None, "new": one}
    assert not os.path.lexists(profile)

    first = _state(tmp_path, cache, "havsorm.profile", profile, f"path={one}")
    assert first["result"] is True, first
    assert first["changes"]["generation"] == profile + "-1-link"
    assert os.readlink(profile) == "salt-1-link"

    same = _state(tmp_path, cache, "havsorm.profile", profile, f"path={one}")
    assert same["changes"] == {}, same

    second = _state(tmp_path, cache, "havsorm.profile", profile, f"path={two}")
    assert second["changes"] == {"old": one, "new": two,
                                 "generation": profile + "-2-link"}, second
    assert os.readlink(profile + "-1-link") == one

    dry = _state(tmp_path, cache, "havsorm.generations_deleted", profile, "test=True")
    assert dry["result"] is None and dry["changes"] == {"deleted": [1]}, dry
    assert os.path.lexists(profile + "-1-link")

    gone = _state(tmp_path, cache, "havsorm.generations_deleted", profile)
    assert gone["changes"] == {"deleted": [1]}, gone
    assert not os.path.lexists(profile + "-1-link")
    assert os.readlink(profile) == "salt-2-link"

    none = _state(tmp_path, cache, "havsorm.generations_deleted", profile)
    assert none["changes"] == {}, none

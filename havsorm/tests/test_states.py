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
    paths = [
        source.add_to_store(f"env-{i}", data, ContentAddressMethod.NAR, HashAlgorithm.SHA256)
        for i, data in enumerate(contents)
    ]
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
    (cfg / "minion").write_text(
        json.dumps(
            {
                "file_client": "local",
                "root_dir": str(tmp_path / "salt"),
                "id": "minion1",
                "havsorm": {
                    "store": str(tmp_path / "minion"),
                    "nix_settings": {"substituters": cache, "require-sigs": "false"},
                },
            }
        )
    )
    done = subprocess.run(
        [
            sys.executable,
            "-c",
            "import salt.scripts; salt.scripts.salt_call()",
            "--local",
            "--config-dir",
            str(cfg),
            "--out",
            "json",
            "--log-level",
            "quiet",
            "state.single",
            fun,
            *args,
        ],
        capture_output=True,
        text=True,
        timeout=300,
    )
    assert done.stdout, done.stderr[-3000:]
    (ret,) = json.loads(done.stdout)["local"].values()
    return ret


def test_realised_substitutes_and_roots_once(tmp_path: pathlib.Path, cached: tuple[str, str]) -> None:
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


def test_a_path_no_cache_has_fails_the_state(tmp_path: pathlib.Path, cached: tuple[str, str]) -> None:
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
    assert second["changes"] == {"old": one, "new": two, "generation": profile + "-2-link"}, second
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


def test_a_unit_links_its_store_file_and_roots_it(tmp_path: pathlib.Path) -> None:
    from huggorm_bindings import ContentAddressMethod, HashAlgorithm, Store

    source = Store(str(tmp_path / "source"))
    holder = source.add_to_store(
        "unit-demo.service", b"[Unit]\nDescription=demo\n", ContentAddressMethod.FLAT, HashAlgorithm.SHA256
    )
    cache = f"file://{tmp_path / 'cache'}?compression=none"
    source.copy_closure(Store(cache), [holder], check_sigs=False)
    unit_file = source.print_store_path(holder)
    units = str(tmp_path / "units")
    root = str(tmp_path / "roots" / "demo.service")
    args = ["demo.service", f"path={unit_file}", f"unit_dir={units}", f"root={root}"]

    dry = _state(tmp_path, cache, "havsorm.unit", *args, "test=True")
    assert dry["result"] is None, dry
    assert not os.path.lexists(os.path.join(units, "demo.service"))

    done = _state(tmp_path, cache, "havsorm.unit", *args)
    assert done["result"] is True, done
    assert done["changes"] == {"unit": {"old": None, "new": unit_file}, "root": root}
    assert os.readlink(os.path.join(units, "demo.service")) == unit_file
    assert os.readlink(root) == unit_file
    assert "nothing was reloaded" in done["comment"]

    again = _state(tmp_path, cache, "havsorm.unit", *args)
    assert again["changes"] == {}, again


def test_a_unit_never_replaces_a_file_it_did_not_make(tmp_path: pathlib.Path) -> None:
    cache, (unit_file,) = _cache(tmp_path, b"[Unit]\n")
    units = tmp_path / "units"
    units.mkdir()
    (units / "demo.service").write_text("hand-written\n")
    ret = _state(
        tmp_path,
        cache,
        "havsorm.unit",
        "demo.service",
        f"path={unit_file}",
        f"unit_dir={units}",
        f"root={tmp_path / 'r' / 'demo'}",
    )
    assert ret["result"] is False, ret
    assert (units / "demo.service").read_text() == "hand-written\n"
    assert not os.path.lexists(tmp_path / "r" / "demo"), "a refused state changes nothing"


def test_garbage_collection_keeps_what_a_root_reaches(tmp_path: pathlib.Path) -> None:
    cache, (kept, dead) = _cache(tmp_path, b"kept", b"dead")
    root = str(tmp_path / "roots" / "kept")
    os.makedirs(os.path.dirname(root))
    assert _state(tmp_path, cache, "havsorm.realised", kept, f"root={root}")["result"]
    assert _state(tmp_path, cache, "havsorm.realised", dead)["result"]
    store = tmp_path / "minion"

    dry = _state(tmp_path, cache, "havsorm.garbage_collected", "gc", "test=True")
    assert dry["result"] is None, dry
    assert dry["changes"] == {"deleted": [dead]}, dry
    assert (store / dead.lstrip("/")).exists()

    roomy = _state(tmp_path, cache, "havsorm.garbage_collected", "gc", "keep_free=1")
    assert roomy["changes"] == {}, roomy

    done = _state(tmp_path, cache, "havsorm.garbage_collected", "gc")
    assert done["result"] is True, done
    assert done["changes"]["deleted"] == [dead]
    assert done["changes"]["bytes_freed"] > 0
    assert not (store / dead.lstrip("/")).exists()
    assert (store / kept.lstrip("/")).exists()

    again = _state(tmp_path, cache, "havsorm.garbage_collected", "gc")
    assert again["changes"] == {}, again


def test_store_present_installs_nix_from_its_release(tmp_path: pathlib.Path) -> None:
    """A fake Nix closure in a cache, and a release file that names it:
    the state reads the release, substitutes Nix, points the default
    profile at it, writes nix.conf and links the daemon's units."""
    from huggorm_bindings import Store, current_system, filesystem_accessor

    fake = tmp_path / "fake-nix"
    (fake / "bin").mkdir(parents=True)
    (fake / "bin" / "nix").write_text("#!/bin/sh\n")
    units = fake / "lib" / "systemd" / "system"
    units.mkdir(parents=True)
    for unit_name in ("nix-daemon.socket", "nix-daemon.service"):
        (units / unit_name).write_text("[Unit]\n")
    source = Store(str(tmp_path / "source"))
    nix = source.add_accessor_to_store("nix-2.99.0", filesystem_accessor(fake))
    cache = f"file://{tmp_path / 'cache'}?compression=none"
    source.copy_closure(Store(cache), [nix], check_sigs=False)
    nix_path = source.print_store_path(nix)
    release = tmp_path / "fallback-paths.nix"
    release.write_text(f'{{ {current_system()} = "{nix_path}"; }}\n')

    conf_dir = tmp_path / "etc-nix"
    unit_dir = tmp_path / "units"
    args = [
        "nix",
        f"release_url=file://{release}",
        f"conf_dir={conf_dir}",
        f"unit_dir={unit_dir}",
        'settings={"build-users-group": "nixbld"}',
    ]

    dry = _state(tmp_path, cache, "havsorm.store_present", *args, "test=True")
    assert dry["result"] is None, dry
    assert not conf_dir.exists()
    assert not (tmp_path / "minion" / "nix/var/nix/daemon-socket").exists()

    done = _state(tmp_path, cache, "havsorm.store_present", *args)
    assert done["result"] is True, done
    profile = tmp_path / "minion" / "nix/var/nix/profiles/default"
    assert os.readlink(os.path.join(profile.parent, os.readlink(profile))) == nix_path
    assert (conf_dir / "nix.conf").read_text() == "build-users-group = nixbld\n"
    assert os.readlink(unit_dir / "nix-daemon.service") == f"{nix_path}/lib/systemd/system/nix-daemon.service"
    store = tmp_path / "minion" / nix_path.lstrip("/")
    assert (store / "bin" / "nix").read_text() == "#!/bin/sh\n"
    assert (tmp_path / "minion" / "nix/var/nix/daemon-socket").is_dir()

    again = _state(tmp_path, cache, "havsorm.store_present", *args)
    assert again["result"] is True and again["changes"] == {}, again

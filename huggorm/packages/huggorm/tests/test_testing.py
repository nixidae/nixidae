"""`huggorm.testing`: the store fixtures other suites load as a plugin."""

import pathlib

import pytest

import huggorm
from huggorm.testing import environment, settings


async def test_a_local_environment_is_a_chroot_store_under_its_root(tmp_path: pathlib.Path) -> None:
    root = tmp_path / "root"
    with environment.nix_environment("local", root, allow_system_store=False) as env:
        assert env.store_uri == f"local://?root={root}"
        async with huggorm.AsyncSession() as session:
            store = session.store(env.store_uri)
            added = await store.add_to_store("fixture", b"fixture\n", method=huggorm.ContentAddressMethod.FLAT)
            path = await store.print_store_path(added)
        assert path.startswith("/nix/store/")
        assert env.physical_path(path).read_bytes() == b"fixture\n"
    assert not root.exists()


@pytest.mark.live
def test_a_daemon_environment_serves_its_store_on_its_own_socket(tmp_path: pathlib.Path) -> None:
    root = tmp_path / "root"
    with environment.nix_environment("daemon", root, allow_system_store=False) as env:
        assert env.store_uri.startswith("unix://")
        assert (root / "nix" / "store").is_dir()
        assert env.store_uri_matches("daemon")
    assert not root.exists()


def test_the_system_store_is_not_relocated(monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path) -> None:
    monkeypatch.setenv(environment.SYSTEM_STORE_ENV, "1")
    with environment.nix_environment("local", tmp_path / "root") as env:
        assert env.store_uri == "local://"
        assert env.physical_path("/nix/store/x") == pathlib.Path("/nix/store/x")


def test_an_unknown_backend_is_refused(tmp_path: pathlib.Path) -> None:
    with (
        pytest.raises(ValueError, match="unknown Nix test backend"),
        environment.nix_environment("ssh", tmp_path),
    ):
        pass


def test_a_setting_change_names_the_old_and_the_new_value() -> None:
    assert settings._changes({"a": "1", "b": "2"}, {"a": "1", "b": "3", "c": "4"}) == [
        "b: '2' -> '3'",
        "c: None -> '4'",
    ]

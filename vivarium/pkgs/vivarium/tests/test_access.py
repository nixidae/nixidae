from __future__ import annotations

import json
from typing import TYPE_CHECKING

from vivarium import access
from vivarium_runner.forward import PortMap, Rule

if TYPE_CHECKING:
    from pathlib import Path

ONE = access.guest(["127.0.0.2:4325"], [Rule(address="127.0.0.2", ports=(PortMap(4325, 4325), PortMap(16443, 6443)))])
DARK = access.guest([], [Rule(address="127.0.0.3")])


def test_the_document_names_each_guest_and_where_the_rest_is(tmp_path: Path) -> None:
    written = json.loads(access.document({"one": ONE, "dark": DARK}, tmp_path))
    assert written["guests"]["one"] == {
        "ssh": "127.0.0.2:4325",
        "forwards": [
            {
                "address": "127.0.0.2",
                "ports": [{"host": 4325, "guest": 4325}, {"host": 16443, "guest": 6443}],
                "protocols": ["tcp"],
            }
        ],
    }
    assert written["guests"]["dark"] == {
        "ssh": None,
        "forwards": [{"address": "127.0.0.3", "ports": "all", "protocols": ["tcp"]}],
    }
    assert written["ssh_config"] == str(tmp_path / "ssh_config")
    assert written["files"] == str(tmp_path / "access")


def test_ssh_config_has_a_host_only_for_a_guest_with_ssh() -> None:
    config = access.ssh_config({"one": ONE, "dark": DARK})
    assert config.splitlines()[:3] == ["Host one", "  HostName 127.0.0.2", "  Port 4325"]
    assert "dark" not in config


def test_a_reused_out_loses_the_last_runs_files(tmp_path: Path) -> None:
    stale = tmp_path / "access" / "kubeconfig"
    stale.parent.mkdir()
    stale.write_text("server: https://127.0.0.9:6443")
    directory = access.write({"one": ONE}, tmp_path)
    assert directory == tmp_path / "access"
    assert list(directory.iterdir()) == []
    assert (tmp_path / "access.json").exists()
    assert (tmp_path / "ssh_config").read_text().startswith("Host one\n")

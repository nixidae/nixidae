"""A `#!nix` reactor SLS, rendered by Salt's own reactor on a master."""

import pathlib
from typing import Any

import salt.config
import salt.utils.reactor


def test_a_reaction_reads_the_event(tmp_path: pathlib.Path) -> None:
    opts: dict[str, Any] = salt.config.master_config(None)
    opts.update(root_dir=str(tmp_path), cachedir=str(tmp_path / "cache"),
                pki_dir=str(tmp_path / "pki"), sock_dir=str(tmp_path / "sock"),
                file_roots={"base": [str(tmp_path / "srv")]})
    reaction = tmp_path / "start.sls"
    reaction.write_text(
        "#!nix\n"
        "{ tag, data, ... }:\n"
        "{ \"sync-${data.id}\".\"local.saltutil.sync_grains\" = [\n"
        "    { tgt = data.id; } { args = [ { event = tag; } ]; } ]; }\n")

    (chunk,) = salt.utils.reactor.Reactor(opts).reactions(
        "salt/minion/web1/start", {"id": "web1"}, [str(reaction)])

    assert chunk["state"] == "local"
    assert chunk["fun"] == "saltutil.sync_grains"
    assert chunk["tgt"] == "web1"
    assert chunk["args"] == [{"event": "salt/minion/web1/start"}]

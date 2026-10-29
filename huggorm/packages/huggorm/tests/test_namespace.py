"""A server in its own user namespace, over an overlay store
(nixidae/nixidae#1060)."""

import json
import os
import pathlib
import sys
import urllib.parse
from collections.abc import AsyncIterator

import anyio
import pytest

from huggorm import Multiprocessing, ServerOptions, Stdio, UnixSocket, Via, worker
from huggorm.namespace import OverlayNamespace, remove_layers
from huggorm_dsl.declare import NIX_2_35

PROBE = """
import sys
from huggorm.namespace import OverlayNamespace, enter_overlay_namespace
root = sys.argv[1]
enter_overlay_namespace(OverlayNamespace.under(root), mount_at=root + "/mnt")
"""


def test_the_layout_and_the_uri() -> None:
    spec = OverlayNamespace.under("/r", lower_store="local://?root=/l", lower_dir="/l/nix/store")
    assert (spec.upper_dir, spec.work_dir, spec.state_dir, spec.log_dir) == (
        "/r/upper",
        "/r/work",
        "/r/state",
        "/r/log",
    )
    scheme, _, query = spec.store_uri().partition("?")
    assert scheme == "local-overlay://"
    assert urllib.parse.parse_qs(query) == {
        "lower-store": ["local://?root=/l"],
        "upper-layer": ["/r/upper"],
        "state": ["/r/state"],
        "log": ["/r/log"],
    }


@pytest.fixture
async def layers(tmp_path: pathlib.Path) -> AsyncIterator[pathlib.Path]:
    """A directory for overlay layers, which pytest alone cannot remove:
    OverlayFS leaves `work/work` at mode 000."""
    yield tmp_path
    await anyio.to_thread.run_sync(remove_layers, tmp_path)


async def _namespaces(tmp_path: pathlib.Path) -> str:
    """Empty when this host lets an unprivileged process mount an
    overlay in a user namespace; otherwise the reason it does not."""
    done = await anyio.run_process([sys.executable, "-c", PROBE, str(tmp_path / "probe")], check=False)
    return done.stderr.decode().strip().splitlines()[-1] if done.returncode else ""


async def _skewed() -> str:
    """Empty unless this build's Nix cannot query the daemon's
    realisations: a 2.35 daemon refuses a 2.34 client's QueryRealisation
    (`worker-protocol.cc:493`), and the overlay asks one after a build."""
    if NIX_2_35:
        return ""
    nix = os.environ.get("HUGGORM_ORACLE_NIX")
    assert nix, "run through `nix run --file . internal.test`, which sets it"
    done = await anyio.run_process([nix, "store", "info", "--store", "daemon", "--json"])
    daemon = json.loads(done.stdout)["version"]
    if tuple(map(int, daemon.split(".")[:2])) < (2, 35):
        return ""
    return f"a Nix 2.34 overlay cannot build over the {daemon} daemon (nixidae/nixidae#1061)"


def _derivation(unique: pathlib.Path) -> str:
    """A derivation the host store cannot hold: `unique` is in it."""
    bash = os.environ.get("HUGGORM_ORACLE_BASH")
    assert bash, "run through `nix run --file . internal.test`, which sets it"
    return (
        f'derivation {{ name = "built"; system = "x86_64-linux"; '
        f'builder = "${{builtins.storePath {bash}}}/bin/bash"; '
        f'args = ["-c" "echo {unique} > $out"]; }}'
    )


@pytest.mark.live
@pytest.mark.parametrize(
    "via", [UnixSocket(), Stdio(), Multiprocessing("forkserver"), Multiprocessing("spawn")], ids=repr
)
async def test_an_overlay_server_builds_into_its_upper_layer_alone(layers: pathlib.Path, via: Via) -> None:
    """A path the host store holds is valid through the overlay, and a
    path the server builds lands in the upper layer, not in the host
    store. `--load-config` reads the host's nix.conf, so the build
    shows that `REQUIRED_SETTINGS` overrides what needs the host's root.

    `unshare` needs a process with one thread. A forkserver child has
    one: a fork copies only the thread that forked.

    The lower layer is the host store: any other hides the server's own
    closure under /nix/store, and the server then fails its first lazy
    import (measured: `No module named 'anyio._backends'`)."""
    if reason := await _namespaces(layers) or await _skewed():
        pytest.skip(reason)
    held = str(pathlib.PurePath(*pathlib.Path(sys.executable).resolve().parts[:4]))
    root = layers / "overlay"
    options = ServerOptions(overlay=root, load_config=True)
    spec = OverlayNamespace.under(root)
    assert options.overlay_namespace() == spec
    async with worker(options, via=via) as session:
        store = await session.store(spec.store_uri())
        assert await store.is_valid_path(await store.parse_store_path(held))
        state = await session.eval(store)
        drv = await (await state.eval_expr(_derivation(layers))).drv_path()
        derived = await store.parse_derived_path(f"{await store.print_store_path(drv)}^*")
        [result] = await store.build_paths_with_results([derived])
        assert result.error() is None
        success = result.success()
        assert success is not None
        [built] = success.built_outputs().values()
        added = await store.print_store_path(built.out_path())
    name = pathlib.PurePath(added).name
    assert await anyio.Path(root / "upper" / name).exists()
    assert not await anyio.Path(added).exists()

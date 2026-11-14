"""A build in another store copies the derivation's closure without a signature check.

`copyDrvsFromEvalStore` copied it with `CheckSigs`, so the build store
refused an input the eval store held unsigned: "lacks a signature by a
trusted key" (measured with pynixd on sg1, 2026-11-14). The build hook
copies the same inputs with `NoCheckSigs`; the daemon still checks an
untrusted client. Patch `nix-eval-store-copy-no-check-sigs`.
"""

from __future__ import annotations

import hashlib
import os
import pathlib
import subprocess

import pytest

from huggorm_dsl.declare import DETERMINATE

pytestmark = pytest.mark.skipif(not DETERMINATE, reason="the patch is the determinate lane's")

INPUT = "/nix/store/00000000000000000000000000000001-unsigned-input"


def _nar_of_a_file(content: bytes) -> bytes:
    def s(data: bytes) -> bytes:
        return len(data).to_bytes(8, "little") + data + b"\0" * (-len(data) % 8)

    return b"".join(s(x) for x in (b"nix-archive-1", b"(", b"type", b"regular", b"contents", content, b")"))


def _register_unsigned(root: pathlib.Path) -> None:
    """An input-addressed path in the eval store, with no signature at all."""
    content = b"unsigned\n"
    (root / INPUT.lstrip("/")).parent.mkdir(parents=True, exist_ok=True)
    (root / INPUT.lstrip("/")).write_bytes(content)
    nar = _nar_of_a_file(content)
    nix = pathlib.Path(os.environ["HUGGORM_ORACLE_NIX"])
    subprocess.run(
        [str(nix.with_name("nix-store")), "--store", f"local?root={root}", "--register-validity", "--hash-given"],
        input=f"{INPUT}\nsha256:{hashlib.sha256(nar).hexdigest()}\n{len(nar)}\n\n0\n".encode(),
        check=True,
        env={**os.environ, "NIX_CONF_DIR": str(root), "NIX_USER_CONF_FILES": ""},
    )


def _realise(tmp_path: pathlib.Path, daemon: str) -> tuple[str, bool]:
    """Build a derivation that takes the unsigned input, in *daemon*: the error, and whether the input arrived."""
    from huggorm_bindings import EvalState, Store
    from huggorm_bindings.errors import NixError

    evals = tmp_path / "evals"
    _register_unsigned(evals)
    builds = Store(daemon)
    state = EvalState(Store(f"local?root={evals}"), None, builds)
    drv = (
        'derivation { name = "uses-unsigned"; system = builtins.currentSystem; '
        f'builder = "/nonexistent"; args = [ (builtins.storePath "{INPUT}") ]; }}'
    )
    # The gate's sandbox runs no builder: the build fails, after the copy.
    with pytest.raises(NixError) as caught:
        state.eval_expr(f'"${{{drv}}}"').realise_string()
    return str(caught.value), builds.is_valid_path(builds.to_store_path(INPUT).path())


def test_an_unsigned_input_reaches_a_build_store_that_trusts_the_client(
    tmp_path: pathlib.Path, trusted_daemon: str
) -> None:
    error, arrived = _realise(tmp_path, trusted_daemon)

    assert "lacks a signature" not in error, error
    assert arrived


def test_a_build_store_still_checks_an_untrusted_client(tmp_path: pathlib.Path, private_daemon: str) -> None:
    """The daemon drops `dontCheckSigs` for an untrusted client (`daemon.cc`)."""
    error, arrived = _realise(tmp_path, private_daemon)

    assert "lacks a signature by a trusted key" in error, error
    assert not arrived

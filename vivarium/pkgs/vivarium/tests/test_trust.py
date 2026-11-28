"""The run's roots in `access/`: the lab root on demand, a test's own at start."""

from __future__ import annotations

import datetime
from pathlib import Path

import pytest
import vivarium_ca
from vivarium import trust
from vivarium.exports import Exports


def _own_root(tmp_path: Path) -> tuple[Path, str]:
    pem = vivarium_ca.authority.root("a test's own root", datetime.datetime.now(datetime.UTC)).certificate_pem()
    path = tmp_path / "own.pem"
    path.write_text(pem)
    return path, pem


@pytest.mark.anyio
async def test_a_run_with_no_roots_writes_nothing_at_start(tmp_path: Path) -> None:
    exported = Exports()
    await trust.Trust("t", tmp_path, tmp_path / "state", exported).start()
    assert not (tmp_path / trust.CERTIFICATE).exists()
    assert exported.values == {}


@pytest.mark.anyio
async def test_own_roots_are_trusted_from_the_start(tmp_path: Path) -> None:
    path, pem = _own_root(tmp_path)
    exported = Exports()
    await trust.Trust("t", tmp_path, tmp_path / "state", exported, [path]).start()
    assert (tmp_path / trust.CERTIFICATE).read_text() == pem
    assert pem in (tmp_path / trust.BUNDLE).read_text()
    assert exported.values["SSL_CERT_FILE"] == str(tmp_path / trust.BUNDLE)
    assert exported.values["NODE_EXTRA_CA_CERTS"] == str(tmp_path / trust.CERTIFICATE)
    assert not (tmp_path / "state").exists(), "no lab root until a phase asks"


@pytest.mark.anyio
async def test_the_lab_root_comes_first_and_keeps_the_own_roots(tmp_path: Path) -> None:
    path, pem = _own_root(tmp_path)
    roots = trust.Trust("t", tmp_path, tmp_path / "state", Exports(), [path])
    await roots.start()
    issuer = await roots.issuer()
    assert (tmp_path / trust.CERTIFICATE).read_text() == issuer.root + pem
    bundle = (tmp_path / trust.BUNDLE).read_text()
    assert issuer.root in bundle
    assert pem in bundle

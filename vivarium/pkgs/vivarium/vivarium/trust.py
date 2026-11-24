"""The run's lab CA, made when a phase first asks for it.

The root lives in the user's state directory (`~/.local/state/vivarium/ca`
on Linux), so a person imports it into a browser once. A sandboxed check
has `HOME` in its build directory, so it makes a root of its own there,
and the root never reaches the store. Its key stays on the host: a lab gets
an intermediate made for this run.

A run that never asks writes nothing, here or in `access/`.
"""

from __future__ import annotations

import datetime
import os
import ssl
from pathlib import Path
from typing import Final

import anyio
import certifi
import platformdirs
import vivarium_ca
from vivarium_runner import Exports, Issuer

CERTIFICATE: Final = "ca.pem"
"""The lab root, for a browser or a program that adds one CA."""
BUNDLE: Final = "ca-bundle.pem"
"""The host's public roots and the lab root, for `SSL_CERT_FILE`."""

BUNDLE_VARIABLES: Final = ("SSL_CERT_FILE", "REQUESTS_CA_BUNDLE", "CURL_CA_BUNDLE")
EXTRA_VARIABLES: Final = ("NODE_EXTRA_CA_CERTS",)
"""Node adds these to its own roots, so they hold the lab root alone."""


def directory() -> Path:
    return platformdirs.user_state_path("vivarium") / "ca"


def system_candidates() -> list[Path]:
    """Where the host keeps its public roots, after the caller's variables.
    certifi last: it is always there, and holds no root the host added."""
    found = [ssl.get_default_verify_paths().cafile, ssl.get_default_verify_paths().openssl_cafile]
    return [Path(each) for each in found if each] + [Path(certifi.where())]


class Trust:
    """`vivarium_runner.Trust` for one run."""

    def __init__(self, run: str, access: Path, state: Path, exports: Exports) -> None:
        self._run = run
        self._access = access
        self._state = state
        self._exports = exports
        self._lock = anyio.Lock()
        self._issuer: Issuer | None = None

    async def issuer(self) -> Issuer:
        async with self._lock:
            if self._issuer is None:
                self._issuer = await anyio.to_thread.run_sync(self._make)
                for name in BUNDLE_VARIABLES:
                    await self._exports.set(name, str(self._access / BUNDLE))
                for name in EXTRA_VARIABLES:
                    await self._exports.set(name, str(self._access / CERTIFICATE))
            return self._issuer

    def _make(self) -> Issuer:
        now = datetime.datetime.now(datetime.UTC)
        root = vivarium_ca.load_or_create(self._state, now)
        run = vivarium_ca.intermediate(root, f"vivarium run {self._run}", now)
        root_pem = root.certificate_pem()
        system = vivarium_ca.system_roots(os.environ, system_candidates())
        (self._access / CERTIFICATE).write_text(root_pem)
        (self._access / BUNDLE).write_text(vivarium_ca.bundle(system.read_text() if system else "", root_pem))
        return Issuer(certificate=run.certificate_pem(), key=run.key_pem(), root=root_pem)

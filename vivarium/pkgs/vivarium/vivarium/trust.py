"""The run's roots: the lab CA, made when a phase first asks for it, and
the test's own (`trust.roots`), published when the run starts.

The lab root lives in the user's state directory
(`~/.local/state/vivarium/ca` on Linux), so a person imports it into a
browser once. A sandboxed check has `HOME` in its build directory, so it
makes a root of its own there, and the root never reaches the store. Its
key stays on the host: a lab gets an intermediate made for this run.

A run that never asks and has no roots of its own writes nothing, here or
in `access/`.
"""

from __future__ import annotations

import datetime
import os
import ssl
from pathlib import Path
from typing import TYPE_CHECKING, Final

import anyio
import certifi
import platformdirs
import vivarium_ca
from vivarium_runner import Exports, Issuer

if TYPE_CHECKING:
    from collections.abc import Sequence

CERTIFICATE: Final = "ca.pem"
"""Every root the run trusts beyond the public ones: the lab root, then
the test's own. For a browser, or a program that adds CAs to its own."""
BUNDLE: Final = "ca-bundle.pem"
"""The host's public roots and `CERTIFICATE`'s, for `SSL_CERT_FILE`."""

BUNDLE_VARIABLES: Final = ("SSL_CERT_FILE", "REQUESTS_CA_BUNDLE", "CURL_CA_BUNDLE")
EXTRA_VARIABLES: Final = ("NODE_EXTRA_CA_CERTS",)
"""Node adds these to its own roots, so they hold `CERTIFICATE` alone."""


def directory() -> Path:
    return platformdirs.user_state_path("vivarium") / "ca"


def system_candidates() -> list[Path]:
    """Where the host keeps its public roots, after the caller's variables.
    certifi last: it is always there, and holds no root the host added."""
    found = [ssl.get_default_verify_paths().cafile, ssl.get_default_verify_paths().openssl_cafile]
    return [Path(each) for each in found if each] + [Path(certifi.where())]


def joined(pems: Sequence[str]) -> str:
    """*pems* one after another, each ending in a newline."""
    return "".join(pem if pem.endswith("\n") else pem + "\n" for pem in pems if pem.strip())


class Trust:
    """`vivarium_runner.Trust` for one run."""

    def __init__(
        self, run: str, access: Path, state: Path, exports: Exports, roots: Sequence[Path] = ()
    ) -> None:
        self._run = run
        self._access = access
        self._state = state
        self._exports = exports
        self._roots = tuple(roots)
        self._lock = anyio.Lock()
        self._issuer: Issuer | None = None

    async def start(self) -> None:
        """The test's own roots in `access/` and the variables, before any
        phase: a shell trusts them whether or not a phase asks for the lab CA."""
        if not self._roots:
            return
        async with self._lock:
            await self._publish(None)

    async def issuer(self) -> Issuer:
        async with self._lock:
            if self._issuer is None:
                now = datetime.datetime.now(datetime.UTC)
                self._issuer = await anyio.to_thread.run_sync(self._make, now)
                await self._publish(self._issuer.root)
            return self._issuer

    async def _publish(self, lab_root: str | None) -> None:
        own = [await anyio.Path(root).read_text() for root in self._roots]
        roots = joined([*([lab_root] if lab_root is not None else []), *own])
        system = vivarium_ca.system_roots(os.environ, system_candidates())
        system_text = await anyio.Path(system).read_text() if system else ""
        await anyio.Path(self._access / CERTIFICATE).write_text(roots)
        await anyio.Path(self._access / BUNDLE).write_text(vivarium_ca.bundle(system_text, roots))
        for name in BUNDLE_VARIABLES:
            await self._exports.set(name, str(self._access / BUNDLE))
        for name in EXTRA_VARIABLES:
            await self._exports.set(name, str(self._access / CERTIFICATE))

    def _make(self, now: datetime.datetime) -> Issuer:
        root = vivarium_ca.load_or_create(self._state, now)
        run = vivarium_ca.intermediate(root, f"vivarium run {self._run}", now)
        return Issuer(certificate=run.certificate_pem(), key=run.key_pem(), root=root.certificate_pem())

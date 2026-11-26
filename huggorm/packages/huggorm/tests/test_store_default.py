"""A store opened without a URI is the one `nix` would open.

libstore reads NIX_REMOTE once, when its settings are built at load, so
each case runs in a fresh interpreter.
"""

from __future__ import annotations

import os
import subprocess
import sys

import pytest

OPEN_DEFAULT = """
import anyio
from huggorm.session import AsyncSession
from huggorm_bindings import Store, load_config
if LOAD:
    load_config()
async def session_uri() -> str:
    async with AsyncSession() as session:
        return await session.store().get_uri()
print(Store().get_uri(), anyio.run(session_uri))
"""


def opened(env: dict[str, str], *, load: bool = False) -> list[str]:
    clean = {k: v for k, v in os.environ.items() if k not in ("NIX_REMOTE", "NIX_CONFIG")}
    script = f"LOAD = {load}\n{OPEN_DEFAULT}"
    done = subprocess.run([sys.executable, "-c", script], env=clean | env, capture_output=True, text=True, check=False)
    if done.returncode != 0:
        pytest.fail(done.stderr)
    return done.stdout.split()


def test_nix_remote_names_the_default_store() -> None:
    assert opened({"NIX_REMOTE": "dummy://"}) == ["dummy://", "dummy://"]


def test_the_store_setting_names_it_after_load_config() -> None:
    assert opened({"NIX_CONFIG": "store = dummy://"}, load=True) == ["dummy://", "dummy://"]

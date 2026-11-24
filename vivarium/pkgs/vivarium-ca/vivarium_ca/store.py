"""The user's root on disk: a directory that holds its certificate and key."""

from __future__ import annotations

import contextlib
import errno
import os
import shutil
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING, Final

from . import authority

if TYPE_CHECKING:
    import datetime

CERTIFICATE: Final = "root.pem"
KEY: Final = "root-key.pem"
COMMON_NAME: Final = "vivarium lab root"


def load_or_create(directory: Path, now: datetime.datetime) -> authority.CertifiedKey:
    """The root in *directory*, made on first use.

    A new root goes to a private directory beside it and is renamed into
    place, so two runs that start at once agree on one root: the rename
    that loses finds the winner's.
    """
    try:
        return _load(directory)
    except FileNotFoundError:
        pass
    directory.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    made = authority.root(COMMON_NAME, now)
    staging = Path(tempfile.mkdtemp(prefix=f".{directory.name}-", dir=directory.parent))
    try:
        _write(staging / CERTIFICATE, made.certificate_pem(), 0o644)
        _write(staging / KEY, made.key_pem(), 0o600)
        staging.rename(directory)
    except OSError as error:
        if error.errno not in (errno.EEXIST, errno.ENOTEMPTY):
            raise
        return _load(directory)
    finally:
        # Gone after a successful rename; the loser's copy otherwise.
        with contextlib.suppress(FileNotFoundError):
            shutil.rmtree(staging)
    return made


def _load(directory: Path) -> authority.CertifiedKey:
    return authority.load((directory / CERTIFICATE).read_text(), (directory / KEY).read_text())


def _write(path: Path, text: str, mode: int) -> None:
    """Made with *mode*, never wider for a moment."""
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
    with os.fdopen(descriptor, "w") as file:
        file.write(text)

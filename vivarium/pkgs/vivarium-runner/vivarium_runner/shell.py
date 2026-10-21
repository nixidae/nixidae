"""A POSIX shell over a serial console: the frame a Linux NOS dialect reuses.

A guest that boots a real Linux (SONiC, Cumulus) answers on a login shell,
not a device CLI. The framing here is that shell's half of the `Dialect`:
a command goes in as base64 so the line editor never sees its bytes, and
the output comes back as base64 between nonce markers, with the exit
status. A dialect pairs this with its own login rules and prompt.

Pure functions over bytes. docs/design/console-guests.md has the design.
"""

from __future__ import annotations

import base64
import re
from typing import Final

from .control import Result

CANON: Final = 4096
"""The terminal's canonical-mode line limit (`N_TTY_BUF_SIZE`). A typed
line at or above it is truncated, so a command whose base64 would cross it
is refused rather than silently cut. A longer command needs chunking
across lines, as `routeros.frame` does through a shell variable; this
frame is one line and refuses instead."""


def frame(command: str, nonce: str) -> list[bytes]:
    """The one line that runs *command* and prints its result.

    The command is base64, decoded and run by a fresh shell, so the line
    editor sees no newline, quote or control byte of the command's own.
    The output comes back base64 between ``<nonce>-B`` and ``<nonce>-D``
    markers, each alone on a line so the command's echo cannot be mistaken
    for them, and the shell's exit status follows the end marker.
    """
    payload = base64.b64encode(command.encode()).decode()
    line = (
        f"o=$(printf %s {payload} | base64 -d | bash 2>&1); r=$?; "
        f"printf '\\n%s\\n%s\\n%s %s\\n' {nonce}-B "
        f'"$(printf %s "$o" | base64 | tr -d \'\\n\')" {nonce}-D "$r"'
    )
    if len(line) >= CANON:
        raise ValueError(f"command of {len(payload)} base64 bytes exceeds the {CANON}-byte line limit")
    return [f"{line}\n".encode()]


def parse(buffer: bytes, nonce: str) -> Result | None:
    """The result of the frame with *nonce*, or `None` while incomplete.

    The markers count only alone on a line: the command's echo carries
    them inside the `printf`, mid-line.
    """
    begin = re_marker(nonce, "B").search(buffer)
    if begin is None:
        return None
    end = re_marker(nonce, "D", status=True).search(buffer, begin.end() - 1)
    if end is None:
        return None
    blob = b"".join(buffer[begin.end() : end.start()].split())
    output = base64.b64decode(blob).decode(errors="replace")
    status = int(end.group("status"))
    if status == 0:
        return Result(ok=True, output=output)
    return Result(ok=False, output=output, error=f"exit status {status}")


def re_marker(nonce: str, letter: str, *, status: bool = False) -> re.Pattern[bytes]:
    tag = re.escape(f"{nonce}-{letter}".encode())
    tail = rb" (?P<status>\d+)" if status else b""
    return re.compile(rb"\r?\n" + tag + tail + rb"\r?\n")

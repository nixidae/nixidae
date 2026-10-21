"""MikroTik RouterOS over its serial console: the dialect ConsoleControl speaks.

Pure functions over bytes; docs/design/console-guests.md has the
measurements behind each choice.
"""

from __future__ import annotations

import base64
import re
from typing import Final

from .control import Dialect, Result, Rule

PASSWORD: Final = "vivarium"
"""The admin password a run sets at the first login. A lab secret: the
guest has no network the host does not give it."""

USER: Final = "admin+ct65535w9999h"
"""No colour, no terminal detection (10 s of the login without it), 65535
columns so nothing wraps, 9999 rows so `print` does not page."""

CONFIG_FILE: Final = "vivarium-config.rsc"
"""The file on the config disk that `config_import` applies."""

CHUNK: Final = 16 * 1024
"""Base64 per typed line. The line editor takes 24 KB and hangs on 76 KB."""

PROMPT: Final = re.compile(rb"\] > $")

LOGIN: Final = (
    Rule(re.compile(rb"Login: $"), f"{USER}\r".encode()),
    Rule(re.compile(rb"Password: $"), b"\r"),
    Rule(re.compile(rb"\[Y/n\]: $"), b"n\r"),
    # Both "new password> " and "repeat new password> ".
    Rule(re.compile(rb"new password> $"), f"{PASSWORD}\r".encode()),
    Rule(PROMPT, None),
)


def _script(source: str, nonce: str) -> str:
    """The script `:execute` runs: decode, run, and on an error print a
    line tagged with the nonce. `source` is a RouterOS expression that
    yields the base64."""
    return (
        f":onerror e in={{ [[:parse [:convert from=base64 {source}]]] }}"
        f' do={{ :put ("{nonce}-E " . $e) }}'
    )


def _quote(text: str) -> str:
    """A RouterOS string literal. Only ever given text made of the
    template, base64 and the nonce, so `\\`, `"` and `$` are all there is."""
    escaped = text.replace("\\", "\\\\").replace('"', '\\"').replace("$", "\\$")
    return f'"{escaped}"'


def frame(command: str, nonce: str) -> list[bytes]:
    """The lines that run *command*; the last one prints the result.

    Each line is a fixed template around base64 and the nonce, so no
    command can break its syntax, and the line editor sees no tab, `?` or
    quote of the command's own. The output comes back as base64 too.
    """
    payload = base64.b64encode(command.encode()).decode()
    lines: list[str] = []
    if len(payload) <= CHUNK:
        script = _script(f'"{payload}"', nonce)
        cleanup = ""
    else:
        name = f"viv{nonce}"
        chunks = [payload[i : i + CHUNK] for i in range(0, len(payload), CHUNK)]
        lines.append(f':global {name} "{chunks[0]}"')
        lines += [f':global {name}; :set {name} (${name} . "{chunk}")' for chunk in chunks[1:]]
        script = f":global {name}; " + _script(f"${name}", nonce)
        cleanup = f'; /system script environment remove [find where name="{name}"]'
    lines.append(
        f':put "{nonce}-B"; :put [:convert to=base64 [:execute script={_quote(script)} as-string]];'
        f' :put "{nonce}-D"{cleanup}'
    )
    return [f"{line}\r".encode() for line in lines]


def parse(buffer: bytes, nonce: str) -> Result | None:
    """The result of the frame with *nonce*, once the prompt is back.

    The markers count only at the start of a line: the echo carries them
    inside quotes.
    """
    begin = re.search(rb"\n\r?" + re.escape(f"{nonce}-B".encode()) + rb"\r\n", buffer)
    if begin is None:
        return None
    end = re.compile(rb"\n\r?" + re.escape(f"{nonce}-D".encode()) + rb"\r\n").search(buffer, begin.end() - 2)
    if end is None or PROMPT.search(buffer, end.end()) is None:
        return None
    blob = b"".join(buffer[begin.end() : end.start()].split())
    text = base64.b64decode(blob).decode(errors="replace").replace("\r\n", "\n")
    head, _, last = text.rpartition("\n")
    tag = f"{nonce}-E "
    if last.startswith(tag):
        return Result(ok=False, output=head, error=last.removeprefix(tag))
    return Result(ok=True, output=text)


def config_import(timeout: float = 30) -> str:
    """The command that applies the config disk's file.

    RouterOS mounts the disk a moment after the console is up, so the
    command waits for the file. A failing line fails the import with
    RouterOS's message.
    """
    find = f'[/file find where name~"{CONFIG_FILE}\\$"]'
    return (
        ":local n 0\n"
        f":while ([:len {find}] = 0 && $n < {int(timeout * 5)}) do={{ :delay 200ms; :set n ($n + 1) }}\n"
        f":local f {find}\n"
        f':if ([:len $f] = 0) do={{ :error "no {CONFIG_FILE} on any disk" }}\n'
        "/import file-name=[/file get ($f->0) name] verbose=yes"
    )


DIALECT: Final = Dialect(
    name="routeros",
    login=LOGIN,
    prompt=PROMPT,
    frame=frame,
    parse=parse,
    configure=config_import(),
)

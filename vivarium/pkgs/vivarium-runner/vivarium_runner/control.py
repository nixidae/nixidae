"""How the host runs a command in a guest: `Control`.

`machine.AgentControl` asks the vivarium agent. `ConsoleControl` drives a
guest that has no agent through a byte stream, its console: it sends a
dialect's frames and reads until the dialect parses a result. One command
at a time; the stream is the only state. docs/design/console-guests.md is
the design.
"""

from __future__ import annotations

import re
import secrets
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING, Final, Protocol

import anyio

if TYPE_CHECKING:
    from anyio.abc import ByteStream


class ControlKind(StrEnum):
    """How the host drives a guest; `vivarium.<guest>` sets it in the spec."""

    AGENT = "agent"
    ROUTEROS = "routeros"


class Control(Protocol):
    """What `Machine.execute` runs a command through."""

    async def execute(
        self, command: str, timeout: float, env: Mapping[str, str] | None = None
    ) -> tuple[int, str]:
        """The command's exit status and output. A console guest has no
        status of its own: 0, or 1 with the guest's message last."""
        ...

    async def aclose(self) -> None: ...


@dataclass(frozen=True)
class Result:
    ok: bool
    output: str
    error: str | None = None
    """The guest's own message, when not `ok`."""


@dataclass(frozen=True)
class Rule:
    """At the login, a screen ending in `pattern` gets `reply`; `None`
    means the login is done."""

    pattern: re.Pattern[bytes]
    reply: bytes | None


@dataclass(frozen=True)
class Dialect:
    name: str
    login: Sequence[Rule]
    prompt: re.Pattern[bytes]
    frame: Callable[[str, str], list[bytes]]
    """The lines that run a command under a nonce. All but the last end at
    a prompt; the last prints the result."""
    parse: Callable[[bytes, str], Result | None]
    """The result in what was read since the last line went out, or
    `None` while it is incomplete."""
    configure: str | None = None
    """The command that applies the config disk, run once after login."""


class ConsoleError(Exception):
    pass


LOGIN_STEPS: Final = 20
"""More screens than any login has. A login that loops (a refused
password brings `Login:` back) fails here instead of forever."""

INTERRUPT: Final = b"\x03"

RECOVER: Final = 5.0
"""Seconds for the prompt after an interrupt. Well inside the 10 s
`Machine` adds over a command's own deadline, so its deadline cannot cut
a recovery short and leave the console out of step unannounced."""


class ConsoleControl:
    """A guest's console, driven by *dialect*.

    *transcript* gets every byte read, as it arrives: the record a failed
    run leaves, and the fixtures for the dialect's tests.
    """

    def __init__(
        self,
        stream: ByteStream,
        dialect: Dialect,
        transcript: Callable[[bytes], Awaitable[object]] | None = None,
    ) -> None:
        self._stream = stream
        self._dialect = dialect
        self._transcript = transcript
        self._lock = anyio.Lock()
        self._buffer = b""
        self._broken: str | None = None

    async def _read(self) -> bytes:
        data = await self._stream.receive()
        if self._transcript is not None:
            await self._transcript(data)
        self._buffer += data
        return data

    async def _until(self, pattern: re.Pattern[bytes]) -> bytes:
        while (match := pattern.search(self._buffer)) is None:
            await self._read()
        seen, self._buffer = self._buffer[: match.end()], self._buffer[match.end() :]
        return seen

    async def login(self, timeout: float) -> None:
        """Answer the login screens until the prompt."""
        rules = self._dialect.login
        with anyio.fail_after(timeout):
            for _ in range(LOGIN_STEPS):
                while (rule := next((r for r in rules if r.pattern.search(self._buffer)), None)) is None:
                    await self._read()
                self._buffer = b""
                if rule.reply is None:
                    return
                await self._stream.send(rule.reply)
        raise ConsoleError(f"{self._dialect.name}: no prompt after {LOGIN_STEPS} login screens")

    async def run(self, command: str, timeout: float) -> Result:
        """Run *command*; its output and whether it failed.

        On a timeout the dialect's command may still be running, so the
        console is interrupted and must show the prompt again before the
        next command; if it does not, every later command fails.
        """
        async with self._lock:
            if self._broken is not None:
                raise ConsoleError(self._broken)
            nonce = f"v{secrets.token_hex(4)}"
            *setup, last = self._dialect.frame(command, nonce)
            try:
                with anyio.fail_after(timeout):
                    for line in setup:
                        await self._stream.send(line)
                        await self._until(self._dialect.prompt)
                    self._buffer = b""
                    await self._stream.send(last)
                    while (result := self._dialect.parse(self._buffer, nonce)) is None:
                        await self._read()
                    self._buffer = b""
                    return result
            except TimeoutError:
                await self._recover()
                raise

    async def execute(
        self, command: str, timeout: float, env: Mapping[str, str] | None = None
    ) -> tuple[int, str]:
        if env:
            raise ConsoleError(f"{self._dialect.name}: a console takes no environment")
        result = await self.run(command, timeout)
        if result.ok:
            return 0, result.output
        return 1, "\n".join(filter(None, [result.output, result.error]))

    async def _recover(self) -> None:
        self._buffer = b""
        try:
            with anyio.fail_after(RECOVER):
                await self._stream.send(INTERRUPT)
                await self._until(self._dialect.prompt)
        except (TimeoutError, anyio.BrokenResourceError, anyio.EndOfStream):
            self._broken = f"{self._dialect.name}: the console did not come back after a timeout"

    async def aclose(self) -> None:
        await self._stream.aclose()

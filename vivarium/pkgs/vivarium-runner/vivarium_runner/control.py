"""How the host runs a command in a guest: `Control`.

`machine.AgentControl` asks the vivarium agent. `ConsoleControl` drives a
guest that has no agent through a byte stream, its console: it sends a
dialect's frames and reads until the dialect parses a result. One command
at a time; the stream is the only state. docs/design/console-guests.md is
the design.
"""

from __future__ import annotations

import functools
import importlib.metadata
import re
import secrets
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Final, Protocol, TypedDict

import anyio

if TYPE_CHECKING:
    from anyio.abc import ByteStream

AGENT: Final = "agent"
"""The vivarium agent, over the socket the backend gives the guest."""

NONE: Final = "none"
"""Nothing to drive: the console's `ready` line says the guest is up, and a
test reaches it over the network."""

ENTRY_POINTS: Final = "vivarium.controls"
"""The entry point group a package names its console dialects in. The
runner's own are there too, in its pyproject.toml."""


class ControlError(Exception):
    """A guest's `vivarium.control` names a kind or a field nobody owns."""


class ControlSpecJson(TypedDict, total=False):
    """`ControlSpec` as Nix wrote it."""

    kind: str
    config: dict[str, Any]


@dataclass(frozen=True)
class ControlSpec:
    """`vivarium.control`: how the host drives the guest.

    The guest kind's module writes it. `config` belongs to the kind, which
    validates it.
    """

    kind: str = AGENT
    config: Mapping[str, Any] = field(default_factory=dict)

    @classmethod
    def from_json(cls, data: ControlSpecJson) -> ControlSpec:
        return cls(kind=data.get("kind", AGENT), config=dict(data.get("config", {})))


def _config(
    guest: str, spec: ControlSpec, fields: frozenset[str], optional: frozenset[str] = frozenset()
) -> Mapping[str, Any]:
    owner = f"{guest}: vivarium.control.config"
    given = set(spec.config)
    if unknown := sorted(given - fields - optional):
        raise ControlError(f"{owner} has fields the {spec.kind} control does not know: {', '.join(unknown)}")
    if missing := sorted(fields - given):
        raise ControlError(f"{owner} lacks fields the {spec.kind} control needs: {', '.join(missing)}")
    return spec.config


@dataclass(frozen=True)
class NoneConfig:
    """`vivarium.control.config` of a guest with no control channel."""

    ready: re.Pattern[str]
    """Found in a console line, says the guest is up."""

    @classmethod
    def of(cls, guest: str, spec: ControlSpec) -> NoneConfig:
        return cls(ready=re.compile(_config(guest, spec, frozenset({"ready"}))["ready"]))


@dataclass(frozen=True)
class ConsoleConfig:
    """`vivarium.control.config` of a guest a console dialect drives."""

    configure: bool = False
    """Run the dialect's `configure` once after the login: the guest kind
    gave the guest a config disk."""

    @classmethod
    def of(cls, guest: str, spec: ControlSpec) -> ConsoleConfig:
        return cls(configure=bool(_config(guest, spec, frozenset(), frozenset({"configure"})).get("configure")))


class Control(Protocol):
    """What `Machine.execute` runs a command through."""

    async def execute(self, command: str, timeout: float, env: Mapping[str, str] | None = None) -> tuple[int, str]:
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

    async def execute(self, command: str, timeout: float, env: Mapping[str, str] | None = None) -> tuple[int, str]:
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


@functools.cache
def dialects() -> dict[str, Dialect]:
    """Every installed console dialect, by the name `vivarium.control.kind` gives."""
    found: dict[str, Dialect] = {}
    for entry in importlib.metadata.entry_points(group=ENTRY_POINTS):
        dialect = entry.load()
        if not isinstance(dialect, Dialect):
            raise ControlError(f"entry point {entry.name} = {entry.value} is not a Dialect")
        if dialect.name != entry.name:
            raise ControlError(
                f"entry point {entry.name} = {entry.value} names a dialect called {dialect.name!r}; the names must match"
            )
        if entry.name in (AGENT, NONE):
            raise ControlError(f"entry point {entry.name} = {entry.value} takes a name the core keeps")
        if entry.name in found:
            raise ControlError(f"two installed packages name a dialect {entry.name!r}")
        found[entry.name] = dialect
    return found


def dialect(guest: str, spec: ControlSpec) -> Dialect:
    """The dialect *spec* names; an error that names the guest if none is installed."""
    found = dialects()
    try:
        return found[spec.kind]
    except KeyError:
        kinds = ", ".join(sorted([AGENT, NONE, *found]))
        raise ControlError(f"{guest}: vivarium.control.kind is {spec.kind!r}; have {kinds}") from None


def validate(guest: str, spec: ControlSpec) -> None:
    """Refuse a control the guest cannot be driven with, before it boots."""
    if spec.kind == AGENT:
        _config(guest, spec, frozenset())
    elif spec.kind == NONE:
        NoneConfig.of(guest, spec)
    else:
        dialect(guest, spec)
        ConsoleConfig.of(guest, spec)

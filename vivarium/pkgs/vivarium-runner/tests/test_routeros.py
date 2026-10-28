"""The RouterOS dialect and ConsoleControl, against what CHR 7.24.5 prints.

The fake console's screens are cut from the probe transcripts
(docs/design/console-guests.md); `fixtures/` holds a real run's console.
"""

from __future__ import annotations

import base64
import re
from collections.abc import Callable
from pathlib import Path

import anyio
import pytest
from anyio.abc import ByteStream
from vivarium_runner import routeros
from vivarium_runner.control import ConsoleControl, ConsoleError, Result

PROMPT = b"\r\n\r\r\r\x1b[9999B[admin@CHR] > "
FRAME_LINE = re.compile(rb':put "(v[0-9a-f]+)-B"; :put \[:convert to=base64 \[:execute script="(.*)" as-string\]\]')
PAYLOAD = re.compile(r'from=base64 \\"([A-Za-z0-9+/=]*)\\"')


def reply(nonce: str, output: bytes) -> bytes:
    """What CHR prints for a frame whose script printed *output*."""
    blob = base64.b64encode(output)
    return b"\r\n\r" + f"{nonce}-B".encode() + b"\r\n" + blob + b"\r\n" + f"{nonce}-D".encode() + b"\r\n" + PROMPT


def test_a_frame_is_one_line_of_template_and_base64() -> None:
    command = ':put "a\tb ? $x \\"q\\""'
    (line,) = routeros.frame(command, "v0a1b2c3d")
    assert line.endswith(b"\r") and b"\t" not in line and b"?" not in line
    script = FRAME_LINE.match(line)
    assert script is not None and script.group(1) == b"v0a1b2c3d"
    (payload,) = PAYLOAD.findall(script.group(2).decode())
    assert base64.b64decode(payload).decode() == command


def test_a_large_command_goes_in_chunks_through_a_global() -> None:
    command = ":put 1\n" * 5000
    lines = routeros.frame(command, "v00000001")
    assert len(lines) > 2
    assert all(len(line) < routeros.CHUNK + 200 for line in lines)
    chunks: list[bytes] = []
    for line in lines[:-1]:
        match = re.search(rb'"([A-Za-z0-9+/=]+)"', line)
        assert match is not None
        chunks.append(match.group(1))
    assert base64.b64decode(b"".join(chunks)).decode() == command
    assert b"$vivv00000001" in lines[-1].replace(b"\\$", b"$")
    assert b"environment remove" in lines[-1]


def test_the_result_needs_the_end_marker_and_the_prompt() -> None:
    whole = reply("v1", b"10")
    for cut in (len(whole) - len(PROMPT), len(whole) - 30, 10):
        assert routeros.parse(whole[:cut], "v1") is None
    assert routeros.parse(whole, "v1") == Result(ok=True, output="10")


def test_the_echo_does_not_count_as_a_marker() -> None:
    (line,) = routeros.frame(":put 1", "v2")
    assert routeros.parse(line[:-1] + b"\r\n" + PROMPT, "v2") is None


def test_an_error_is_the_last_line() -> None:
    got = routeros.parse(reply("v3", b"before\r\nv3-E bad command name bogus (line 2 column 2)"), "v3")
    assert got == Result(ok=False, output="before", error="bad command name bogus (line 2 column 2)")


def test_output_keeps_its_bytes() -> None:
    text = "".join(f"line number {i}\r\n" for i in range(2000)) + "tab\there"
    got = routeros.parse(reply("v4", text.encode()), "v4")
    assert got == Result(ok=True, output=text.replace("\r\n", "\n"))


def test_empty_output() -> None:
    assert routeros.parse(reply("v5", b""), "v5") == Result(ok=True, output="")


class FakeChr(ByteStream):
    """CHR's console as the probe saw it: the login screens, then each
    frame answered by *answer*, which gets the decoded command."""

    def __init__(self, answer: Callable[[str], tuple[bool, str]]) -> None:
        self._answer = answer
        self._out = b"\x1b[1;1H\x1b[H\x1b[J\x1bcMikroTik 7.24.5 (stable)\r\r\nCHR Login: "
        self._ready = anyio.Event()
        self._ready.set()
        self.sent: list[bytes] = []
        self._globals: dict[str, str] = {}

    async def receive(self, max_bytes: int = 65536) -> bytes:
        while not self._out:
            self._ready = anyio.Event()
            await self._ready.wait()
        # A few bytes at a time, as the serial port gives them.
        chunk, self._out = self._out[:7], self._out[7:]
        return chunk

    def _say(self, data: bytes) -> None:
        self._out += data
        self._ready.set()

    async def send(self, item: bytes) -> None:
        self.sent.append(item)
        line = item.removesuffix(b"\r")
        if line.startswith(b"admin+"):
            self._say(line + b"\r\nPassword: ")
        elif line == b"":
            self._say(
                b"\r\n" * 3 + b"  MikroTik RouterOS 7.24.5\r\n\r\nDo you want to see the software license? [Y/n]: "
            )
        elif line == b"n":
            self._say(b"n\r\n\nChange your password (Ctrl-C to skip)\r\n\r\r\r\x1b[9999Bnew password> ")
        elif line == routeros.PASSWORD.encode() and not self._globals.get("pw"):
            self._globals["pw"] = "1"
            self._say(b"********\r\n\r\r\r\r\x1b[9999Brepeat new password> ")
        elif line == routeros.PASSWORD.encode():
            self._say(b"********\r\n\r\r\nPassword changed" + PROMPT)
        elif m := re.match(rb':global (viv\w+)(?:; :set \1 \(\$\1 \. )? ?"([A-Za-z0-9+/=]+)"', line):
            name = m.group(1).decode()
            self._globals[name] = self._globals.get(name, "") + m.group(2).decode()
            self._say(line + PROMPT)
        elif m := FRAME_LINE.match(line):
            nonce, script = m.group(1).decode(), m.group(2).decode()
            literal = PAYLOAD.findall(script)
            payload = literal[0] if literal else next(v for k, v in self._globals.items() if k.startswith("viv"))
            ok, output = self._answer(base64.b64decode(payload).decode())
            if not ok:
                output = "\r\n".join(filter(None, [output, f"{nonce}-E failure"]))
            self._say(line + reply(nonce, output.encode()))
        elif line == b"\x03":
            self._say(PROMPT)
        else:
            self._say(line + b"\r\n\rbad command name" + PROMPT)

    async def send_eof(self) -> None:
        pass

    async def aclose(self) -> None:
        pass


@pytest.mark.anyio
async def test_login_answers_every_screen_then_runs() -> None:
    chr_ = FakeChr(lambda command: (True, f"ran {command}"))
    seen = bytearray()

    async def record(data: bytes) -> None:
        seen.extend(data)

    console = ConsoleControl(chr_, routeros.DIALECT, transcript=record)
    await console.login(timeout=5)
    assert chr_.sent[:5] == [b"admin+ct65535w9999h\r", b"\r", b"n\r", b"vivarium\r", b"vivarium\r"]
    assert await console.run("/system identity print", timeout=5) == Result(True, "ran /system identity print")
    assert b"CHR Login: " in seen


@pytest.mark.anyio
async def test_a_failure_carries_the_message() -> None:
    console = ConsoleControl(FakeChr(lambda command: (False, "partial")), routeros.DIALECT)
    await console.login(timeout=5)
    assert await console.run("/bogus", timeout=5) == Result(False, "partial", "failure")


@pytest.mark.anyio
async def test_a_large_command_arrives_whole() -> None:
    command = ":put 1\n" * 5000
    console = ConsoleControl(FakeChr(lambda got: (True, str(got == command))), routeros.DIALECT)
    await console.login(timeout=5)
    assert await console.run(command, timeout=5) == Result(True, "True")


class Silent(FakeChr):
    """Answers the login, then never answers a frame."""

    async def send(self, item: bytes) -> None:
        if FRAME_LINE.match(item):
            self.sent.append(item)
            return
        await super().send(item)


@pytest.mark.anyio
async def test_a_timeout_interrupts_and_the_console_recovers() -> None:
    chr_ = Silent(lambda command: (True, ""))
    console = ConsoleControl(chr_, routeros.DIALECT)
    await console.login(timeout=5)
    with pytest.raises(TimeoutError):
        await console.run(":delay 1h", timeout=0.2)
    assert chr_.sent[-1] == b"\x03"


@pytest.mark.anyio
async def test_a_login_that_loops_fails() -> None:
    class Refusing(FakeChr):
        async def send(self, item: bytes) -> None:
            self.sent.append(item)
            self._say(b"\r\nLogin failed\r\nCHR Login: ")

    with pytest.raises(ConsoleError):
        await ConsoleControl(Refusing(lambda c: (True, "")), routeros.DIALECT).login(timeout=5)


TRANSCRIPT = Path(__file__).parent / "fixtures" / "chr-7.24.5-console.raw"
"""`console.raw` from a passing `routeros` run: CHR 7.24.5 under QEMU."""


def recorded() -> dict[str, Result]:
    """Every frame in the transcript, parsed from its echo to the next."""
    data = TRANSCRIPT.read_bytes()
    starts = [(m.start(), m.group(1).decode()) for m in FRAME_LINE.finditer(data)]
    ends = [start for start, _ in starts[1:]] + [len(data)]
    results = {}
    for (start, nonce), end in zip(starts, ends, strict=False):
        result = routeros.parse(data[start:end], nonce)
        assert result is not None, f"frame {nonce} did not parse"
        results[nonce] = result
    return results


def test_every_recorded_frame_parses() -> None:
    results = list(recorded().values())
    assert len(results) == 6
    imported, identity, address, bad, lines, version = results
    assert imported.ok and "#line 1" in imported.output
    assert identity == Result(True, "r1")
    assert "10.255.0.1/32" in address.output
    assert not bad.ok and bad.error is not None and bad.error.startswith("invalid value for argument address")
    assert lines.output.splitlines() == [f"line {i}" for i in range(1, 3001)]
    assert version.output.startswith("7.24.5")


def test_the_recorded_login_reaches_the_prompt() -> None:
    data = TRANSCRIPT.read_bytes()
    login = data[: data.index(b"Password changed")]
    for rule in routeros.LOGIN[:-1]:
        assert rule.pattern.pattern.rstrip(b"$").replace(b"\\", b"") in login


def test_the_config_import_waits_for_the_file() -> None:
    command = routeros.config_import(timeout=10)
    assert ":while" in command and ":delay 200ms" in command and "< 50" in command
    assert command.endswith("verbose=yes")

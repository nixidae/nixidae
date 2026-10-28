"""The SONiC dialect and the shell framing it reuses.

A fake Linux console: a login, then each frame answered from the decoded
command. No guest, so these run as a package check.
"""

from __future__ import annotations

import base64
import re
from collections.abc import Callable

import anyio
import pytest
from anyio.abc import ByteStream
from vivarium_runner import shell, sonic
from vivarium_runner.control import ConsoleControl, Result

PROMPT = b"\r\nadmin@sonic:~$ "
CMD = re.compile(rb"printf %s (\S+) \| base64 -d \| bash")
NONCE = re.compile(rb"printf '[^']*' (\w+)-B ")


def reply(nonce: str, output: bytes, status: int) -> bytes:
    """What the shell prints for a frame: the markers, the output as
    base64 on one line, and the exit status after the end marker."""
    blob = base64.b64encode(output)
    return (
        b"\r\n" + f"{nonce}-B".encode() + b"\r\n" + blob + b"\r\n" + f"{nonce}-D {status}".encode() + b"\r\n" + PROMPT
    )


def test_a_frame_is_one_line_of_shell_and_base64() -> None:
    command = "echo 'a\tb' > /x; grep -c . <<'EOF'\nhi\nEOF"
    (line,) = shell.frame(command, "v0a1b2c3d")
    assert line.endswith(b"\n") and line.count(b"\n") == 1
    assert b"\t" not in line and b"grep" not in line and b"EOF" not in line
    match = CMD.search(line)
    assert match is not None
    assert base64.b64decode(match.group(1)).decode() == command


def test_a_command_too_long_for_a_line_is_refused() -> None:
    with pytest.raises(ValueError, match="line limit"):
        shell.frame("x" * shell.CANON, "v1")


def test_the_result_needs_both_markers() -> None:
    whole = reply("v1", b"42\n", 0)
    for cut in (len(whole) - len(PROMPT) - 5, 8):
        assert shell.parse(whole[:cut], "v1") is None
    assert shell.parse(whole, "v1") == Result(ok=True, output="42\n")


def test_the_echo_does_not_count_as_a_marker() -> None:
    (line,) = shell.frame("echo hi", "v2")
    # The echoed command line carries "v2-B" inside the printf, mid-line.
    assert shell.parse(line.rstrip(b"\n") + b"\r\n" + PROMPT, "v2") is None


def test_a_nonzero_status_is_a_failure() -> None:
    got = shell.parse(reply("v3", b"grep: x: No such file\n", 2), "v3")
    assert got == Result(ok=False, output="grep: x: No such file\n", error="exit status 2")


def test_output_keeps_its_bytes() -> None:
    text = "".join(f"line number {i}\n" for i in range(2000)) + "tab\there"
    got = shell.parse(reply("v4", text.encode(), 0), "v4")
    assert got == Result(ok=True, output=text)


def test_empty_output() -> None:
    assert shell.parse(reply("v5", b"", 0), "v5") == Result(ok=True, output="")


def test_the_motd_last_login_is_not_a_login_prompt() -> None:
    rule = sonic.LOGIN[0]
    # getty's prompt fires; a read ending on the MOTD's `Last login: `
    # colon-space must not.
    assert rule.pattern.search(b"\r\nsonic login: ") is not None
    assert rule.pattern.search(b"\r\nLast login: ") is None


class FakeSonic(ByteStream):
    """A Linux console: `sonic login:`, a password, then each frame
    answered by *answer*, which gets the decoded command and returns
    `(status, output)`."""

    def __init__(self, answer: Callable[[str], tuple[int, str]]) -> None:
        self._answer = answer
        self._out = b"\r\nDebian GNU/Linux 12 sonic ttyS0\r\n\r\nsonic login: "
        self._ready = anyio.Event()
        self._ready.set()
        self.sent: list[bytes] = []
        self._in = False

    async def receive(self, max_bytes: int = 65536) -> bytes:
        while not self._out:
            self._ready = anyio.Event()
            await self._ready.wait()
        chunk, self._out = self._out[:7], self._out[7:]
        return chunk

    def _say(self, data: bytes) -> None:
        self._out += data
        self._ready.set()

    async def send(self, item: bytes) -> None:
        self.sent.append(item)
        line = item.removesuffix(b"\r")
        if line == sonic.USER.encode():
            self._say(line + b"\r\nPassword: ")
        elif line == sonic.PASSWORD.encode():
            self._say(b"\r\nLinux sonic 6.1.0 x86_64\r\nLast login: now" + PROMPT)
        elif cmd := CMD.search(line):
            nonce_match = NONCE.search(line)
            assert nonce_match is not None
            nonce = nonce_match.group(1).decode()
            status, output = self._answer(base64.b64decode(cmd.group(1)).decode())
            self._say(line + reply(nonce, output.encode(), status))
        elif line == b"\x03":
            self._say(PROMPT)
        else:
            self._say(line + b"\r\nbash: command not found" + PROMPT)

    async def send_eof(self) -> None:
        pass

    async def aclose(self) -> None:
        pass


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.mark.anyio
async def test_login_then_run_a_command() -> None:
    fake = FakeSonic(lambda command: (0, f"ran: {command}"))
    console = ConsoleControl(fake, sonic.DIALECT)
    await console.login(timeout=5)
    assert any(item == f"{sonic.USER}\r".encode() for item in fake.sent)
    status, output = await console.execute("uname -sr", timeout=5)
    assert status == 0 and output == "ran: uname -sr"


@pytest.mark.anyio
async def test_a_failing_command_carries_its_status() -> None:
    fake = FakeSonic(lambda command: (7, "boom"))
    console = ConsoleControl(fake, sonic.DIALECT)
    await console.login(timeout=5)
    status, output = await console.execute("false", timeout=5)
    assert status == 1 and "boom" in output and "exit status 7" in output

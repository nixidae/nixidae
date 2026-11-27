"""checks.browser `fetch`: Chromium and Firefox as `vivarium browser` prepares them load lab pages by name.

Headless, with the run's split proxy and the lab root in their NSS
databases (vivarium/browser.py). https://web.test/ is port 443.
https://alt.test/ has only an SRV record, for web.test:8443, which the
router's proxy follows. Negative control: each browser with no lab root
gets no page.

Chromium dumps the page (`--dump-dom`). Firefox has no such flag, so the
phase reads the page through Marionette, Firefox's own remote protocol:
plain TCP, `<length>:<json>` frames.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import tempfile
from pathlib import Path

import anyio
import anyio.abc
from anyio.streams.buffered import BufferedByteReceiveStream
from anyio.streams.text import TextReceiveStream
from vivarium import browser, exports, trust
from vivarium_runner import Machines

PLAIN = "https://web.test/"
MOVED = "https://alt.test/"
LISTENING = re.compile(r"Listening on port (\d+)")


def page_on(port: int) -> str:
    return f"vivarium lab page on {port}"


async def chromium_page(
    chromium: str, certutil: str, proxy: tuple[str, int], root: Path | None, url: str
) -> tuple[str, str]:
    """The page Chromium dumps, and the last lines it wrote to stderr."""
    # --no-sandbox: Chromium's own sandbox needs privileges a Nix build lacks.
    headless = browser.Browser(
        browser.Family.CHROMIUM, (chromium, "--headless=new", "--no-sandbox", "--disable-gpu"), "chromium"
    )
    with tempfile.TemporaryDirectory(prefix="vivarium-browser-") as home:
        argv, extra = browser.prepare(headless, Path(home), proxy, root, "linux", certutil)
        with anyio.fail_after(120):
            done = await anyio.run_process([*argv, "--dump-dom", url], env=os.environ | extra, check=False)
    said = done.stderr.decode(errors="replace").splitlines()[-15:]
    return done.stdout.decode(errors="replace"), "\n".join(said)


class MarionetteError(Exception):
    def __init__(self, kind: str, message: str) -> None:
        super().__init__(f"{kind}: {message}")
        self.kind = kind


class Marionette:
    def __init__(self, stream: anyio.abc.SocketStream) -> None:
        self.stream = stream
        self.reader = BufferedByteReceiveStream(stream)
        self.sent = 0

    async def frame(self) -> object:
        length = await self.reader.receive_until(b":", 16)
        return json.loads(await self.reader.receive_exactly(int(length)))

    async def call(self, command: str, params: dict[str, object]) -> dict[str, object]:
        self.sent += 1
        body = json.dumps([0, self.sent, command, params]).encode()
        await self.stream.send(f"{len(body)}:".encode() + body)
        reply = await self.frame()
        if not isinstance(reply, list) or len(reply) != 4:
            raise AssertionError(f"Marionette answered {command} with {reply!r}")
        _, _, error, result = reply
        if isinstance(error, dict):
            raise MarionetteError(str(error.get("error")), str(error.get("message")))
        return result if isinstance(result, dict) else {"value": result}


async def listening(lines: TextReceiveStream, said: list[str]) -> int:
    """The port Marionette reports on stdout; *said* keeps every line."""
    pending = ""
    async for chunk in lines:
        pending += chunk
        *whole, pending = pending.split("\n")
        said += whole
        for line in whole:
            if found := LISTENING.search(line):
                return int(found.group(1))
    raise AssertionError("Firefox ended before Marionette listened:\n" + "\n".join(said[-15:]))


async def drain(lines: TextReceiveStream, said: list[str]) -> None:
    async for chunk in lines:
        said += chunk.splitlines()


async def firefox_page(
    firefox: str, certutil: str, proxy: tuple[str, int], root: Path | None, url: str
) -> tuple[str, str]:
    """The page Firefox shows, or the error Marionette gives for the navigation, and Firefox's last output lines."""
    headless = browser.Browser(browser.Family.FIREFOX, (firefox, "--headless", "--marionette"), "firefox")
    said: list[str] = []
    with tempfile.TemporaryDirectory(prefix="vivarium-browser-") as home:
        argv, extra = browser.prepare(headless, Path(home), proxy, root, "linux", certutil)
        with (Path(home) / "profile" / "user.js").open("a") as prefs:
            # 0: Marionette takes a free port and says which.
            prefs.write('user_pref("marionette.port", 0);\n')
        # The sandbox's HOME is not writable; Firefox writes caches there.
        env = os.environ | extra | {"HOME": home}
        async with (
            await anyio.open_process(argv, env=env, stdin=subprocess.DEVNULL, stderr=subprocess.STDOUT) as process,
            anyio.create_task_group() as group,
        ):
            if process.stdout is None:
                raise AssertionError("no stdout pipe")
            lines = TextReceiveStream(process.stdout, errors="replace")
            try:
                with anyio.fail_after(120):
                    port = await listening(lines, said)
                    group.start_soon(drain, lines, said)
                    async with await anyio.connect_tcp("127.0.0.1", port) as stream:
                        remote = Marionette(stream)
                        await remote.frame()
                        await remote.call(
                            "WebDriver:NewSession", {"capabilities": {"alwaysMatch": {"acceptInsecureCerts": False}}}
                        )
                        try:
                            await remote.call("WebDriver:Navigate", {"url": url})
                            page = str((await remote.call("WebDriver:GetPageSource", {})).get("value"))
                        except MarionetteError as error:
                            page = f"error: {error}"
            finally:
                with anyio.CancelScope(shield=True), anyio.move_on_after(10):
                    process.terminate()
                    await process.wait()
                group.cancel_scope.cancel()
    return page, "\n".join(said[-15:])


async def test(vms: Machines) -> None:
    if vms.exports is None:
        raise AssertionError("the run exports nothing")
    proxy = browser.socks_endpoint(vms.exports.values[exports.PROXY])
    certutil = vms.settings["certutil"]
    root = vms.access / trust.CERTIFICATE
    web = vms["web"]
    for name, load in (("Chromium", chromium_page), ("Firefox", firefox_page)):
        program = vms.settings[name.lower()]
        for url, port in ((PLAIN, 443), (MOVED, 8443)):
            page, said = await load(program, certutil, proxy, root, url)
            if page_on(port) not in page:
                raise AssertionError(
                    f"{name} with the lab root did not load {url} from port {port}:\n{said}\n{page[-300:]}"
                )
            web.say(f"{name} loads {url} from port {port}, trusting the lab root")
        page, _ = await load(program, certutil, proxy, None, PLAIN)
        if page_on(443) in page:
            raise AssertionError(f"{name} without the lab root loaded {PLAIN}")
        web.say(f"without the lab root, {name} refuses it: {page[:120]!r}")

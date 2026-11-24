"""`vivarium env` and `vivarium shell`: a host shell that reaches a live run.

The run's phases name the variables (`access/environment.json`). This
adds the environment's name, and turns the run's proxy into the variables
programs read. The proxy is the session's split proxy on the host: lab
names and addresses go through the router, the rest straight out. A
program that cannot speak SOCKS fails with any of them set (httpx without
`socksio` refuses every URL), so `unproxy` leads `PATH`: `unproxy CMD`
runs CMD without them, and the Nix commands run that way on their own.
"""

from __future__ import annotations

import os
import pwd
import shlex
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Final

from vivarium import access, exports, places

if TYPE_CHECKING:
    from collections.abc import Mapping

ENVIRONMENT: Final = "VIVARIUM_ENVIRONMENT"
OUT: Final = "VIVARIUM_OUT"

PROXY_VARIABLES: Final = ("ALL_PROXY", "all_proxy", "HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy")
"""curl reads `http_proxy` in lower case only; Go and Python read both."""
NO_PROXY_VARIABLES: Final = ("NO_PROXY", "no_proxy")
NO_PROXY: Final = "localhost,127.0.0.0/8,::1"
"""The host's own forwards, a kubeconfig's API server among them. Go leaves
loopback alone by itself; curl does not."""

UNPROXY: Final = "unproxy"
UNPROXIED: Final = (
    "nix",
    "nix-build",
    "nix-shell",
    "nix-store",
    "nix-env",
    "nix-instantiate",
    "nix-prefetch-url",
    "nix-channel",
    "nix-collect-garbage",
    "pynix",
    "vivarium",
)
"""Commands that fetch from public caches and forges, and never from the
lab: the proxy is a detour for them."""

BIN: Final = "bin"
"""In the runtime directory: `unproxy` and its links."""

WRAPPER: Final = """#!{python} -I
import os
import sys

here = os.path.dirname(os.path.abspath(__file__))
for name in {strip!r}:
    os.environ.pop(name, None)
os.environ["PATH"] = os.pathsep.join(
    each for each in os.environ.get("PATH", "").split(os.pathsep) if os.path.abspath(each or ".") != here
)
called = os.path.basename(sys.argv[0])
argv = sys.argv[1:] if called == {unproxy!r} else [called, *sys.argv[1:]]
if not argv:
    sys.exit("usage: unproxy COMMAND [ARGUMENT...]: run COMMAND without the lab's proxy")
try:
    os.execvp(argv[0], argv)
except OSError as error:
    sys.exit(f"unproxy: {{argv[0]}}: {{error.strerror}}")
"""


class ShellError(RuntimeError):
    pass


def variables(
    exported: Mapping[str, str],
    *,
    environment: str | None,
    out: Path,
    proxied: bool,
    wrappers: Path | None,
    path: str,
) -> dict[str, str]:
    """What `env` prints and `shell` sets. *wrappers* is `unproxy`'s
    directory, put first on *path* when the run serves a proxy."""
    found = dict(exported)
    found[OUT] = str(out)
    if environment is not None:
        found[ENVIRONMENT] = environment
    proxy = exported.get(exports.PROXY)
    if proxied and proxy is not None:
        found |= dict.fromkeys(PROXY_VARIABLES, proxy) | dict.fromkeys(NO_PROXY_VARIABLES, NO_PROXY)
        if wrappers is not None:
            found["PATH"] = os.pathsep.join([str(wrappers), *([path] if path else [])])
    return found


def script(found: Mapping[str, str]) -> str:
    """`export NAME=VALUE` lines, which bash, zsh and fish all read."""
    return "".join(f"export {name}={shlex.quote(value)}\n" for name, value in sorted(found.items()))


def wrappers(root: Path, python: str) -> Path:
    """`<runtime>/bin`: `unproxy`, and a link to it for each of `UNPROXIED`.
    Written on each call: the interpreter is this package's, and a newer
    package may move it."""
    directory = places.private(root / BIN)
    text = WRAPPER.format(python=python, strip=(*PROXY_VARIABLES, *NO_PROXY_VARIABLES), unproxy=UNPROXY)
    target = directory / UNPROXY
    if not target.exists() or target.read_text() != text:
        partial = directory / f".{UNPROXY}.partial"
        partial.write_text(text)
        partial.chmod(0o755)
        partial.replace(target)
    for name in UNPROXIED:
        link = directory / name
        if not link.is_symlink():
            link.unlink(missing_ok=True)
            link.symlink_to(UNPROXY)
    return directory


def gather(out: Path, *, proxied: bool) -> dict[str, str]:
    try:
        exported = exports.read(out / access.DIRECTORY)
    except (OSError, exports.ExportError) as error:
        raise ShellError(str(error)) from None
    wrapped = wrappers(places.runtime(), sys.executable) if proxied and exports.PROXY in exported else None
    return variables(
        exported,
        environment=places.environment_name(out),
        out=out,
        proxied=proxied,
        wrappers=wrapped,
        path=os.environ.get("PATH", ""),
    )


def login_shell() -> str:
    return os.environ.get("SHELL") or pwd.getpwuid(os.getuid()).pw_shell or "/bin/sh"


def enter(found: Mapping[str, str], command: list[str]) -> None:
    """Become *command*, or the user's shell, with *found* set."""
    argv = command or [login_shell()]
    try:
        os.execvpe(argv[0], argv, os.environ | dict(found))
    except OSError as error:
        raise ShellError(f"{argv[0]}: {error.strerror}") from None

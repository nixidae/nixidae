"""`vivarium browser`: the user's own browser, into a live run's lab. nixidae/nixidae#1656.

The browser gets a profile of its own for this launch, so it runs beside
the user's open browser and leaves that profile alone. It reaches the lab
through the session's split proxy (`VIVARIUM_PROXY`): SOCKS5 with remote
names, so the lab resolves its own names and the browser resolves none.
It trusts the lab root (`access/ca.pem`) from a real certificate store,
where the root's name constraints hold: Firefox's profile, and Chromium's
NSS database under a home of its own on Linux. Not Chromium's
`--ignore-certificate-errors-spki-list`: measured, it also accepts a lab
certificate for `example.com`.

The default browser is the `https` handler: `mimeapps.list` on Linux,
LaunchServices on macOS. A Chromium or Firefox family browser runs as it
is; another (Safari) falls back to the first such browser on PATH.
"""

from __future__ import annotations

import configparser
import json
import os
import plistlib
import shlex
import shutil
import subprocess
import sys
import tempfile
import urllib.parse
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING, Final

from . import access, exports, trust

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence


class BrowserError(RuntimeError):
    pass


class Family(StrEnum):
    CHROMIUM = "chromium"
    FIREFOX = "firefox"


FAMILY_NAMES: Final[Mapping[Family, tuple[str, ...]]] = {
    Family.CHROMIUM: ("chromium", "chrome", "brave", "vivaldi", "microsoft-edge", "msedge"),
    Family.FIREFOX: ("firefox", "librewolf", "waterfox", "floorp", "zen"),
}
"""What a desktop file, program or bundle id of each family contains."""

FALLBACK: Final = ("chromium", "chromium-browser", "google-chrome-stable", "google-chrome", "brave", "firefox")
"""Tried on PATH, in order, when the default browser is of neither family."""

HTTPS_HANDLER: Final = "x-scheme-handler/https"
LAUNCH_SERVICES: Final = Path("Library/Preferences/com.apple.LaunchServices/com.apple.launchservices.secure.plist")
LAB_ROOT_NICKNAME: Final = "vivarium lab root"

FIREFOX_PREFS: Final = {
    "network.proxy.type": 1,
    "network.proxy.socks_version": 5,
    "network.proxy.socks_remote_dns": True,
    "network.proxy.no_proxies_on": "localhost, 127.0.0.1, ::1",
    # A DNS-over-HTTPS resolver would answer for lab names around the proxy.
    "network.trr.mode": 5,
    "browser.shell.checkDefaultBrowser": False,
    "browser.aboutwelcome.enabled": False,
    "datareporting.policy.dataSubmissionEnabled": False,
    "browser.startup.homepage_override.mstone": "ignore",
}


@dataclass(frozen=True, slots=True)
class Browser:
    family: Family
    argv: tuple[str, ...]
    """The program, and what comes before this launch's own arguments (macOS: `open -n -W -b ID --args`)."""
    name: str


def family_of(name: str) -> Family | None:
    lowered = name.lower()
    for family, names in FAMILY_NAMES.items():
        if any(each in lowered for each in names):
            return family
    return None


def socks_endpoint(proxy: str) -> tuple[str, int]:
    """`socks5h://127.0.0.1:1080` -> ("127.0.0.1", 1080)."""
    parsed = urllib.parse.urlsplit(proxy)
    if parsed.scheme not in {"socks5", "socks5h"} or parsed.hostname is None or parsed.port is None:
        raise BrowserError(f"the run's proxy is not a SOCKS5 address: {proxy}")
    return parsed.hostname, parsed.port


# ── finding the default ────────────────────────────────────────────


def mimeapps_lists(environ: Mapping[str, str], home: Path) -> list[Path]:
    """The `mimeapps.list` files in the order the XDG spec reads them, each
    desktop's own (`gnome-mimeapps.list`) before the plain one."""
    config_home = Path(environ.get("XDG_CONFIG_HOME") or home / ".config")
    config_dirs = [Path(each) for each in (environ.get("XDG_CONFIG_DIRS") or "/etc/xdg").split(":") if each]
    data_home = Path(environ.get("XDG_DATA_HOME") or home / ".local/share")
    data_dirs = [Path(each) for each in (environ.get("XDG_DATA_DIRS") or "/usr/local/share:/usr/share").split(":") if each]
    desktops = [each.lower() for each in environ.get("XDG_CURRENT_DESKTOP", "").split(":") if each]
    directories = [config_home, *config_dirs, data_home / "applications", *(each / "applications" for each in data_dirs)]
    return [
        directory / name
        for directory in directories
        for name in [*(f"{desktop}-mimeapps.list" for desktop in desktops), "mimeapps.list"]
    ]


def https_handlers(lists: Sequence[Path]) -> list[str]:
    """Every `Default Applications` entry for https among *lists*, in the order they are preferred."""
    found: list[str] = []
    for path in lists:
        if not path.is_file():
            continue
        parser = configparser.ConfigParser(interpolation=None, strict=False)
        parser.optionxform = str  # type: ignore[assignment,method-assign]  # keys are MIME types, case kept
        try:
            parser.read(path)
        except configparser.Error:
            continue
        if parser.has_option("Default Applications", HTTPS_HANDLER):
            found += [each.strip() for each in parser.get("Default Applications", HTTPS_HANDLER).split(";") if each.strip()]
    return found


def desktop_exec(desktop_id: str, environ: Mapping[str, str], home: Path) -> list[str] | None:
    """The program of *desktop_id*'s `Exec=` line, field codes such as `%U` dropped. None when
    its program is not there, as for `TryExec`: a profile's entry can outlive its store path."""
    data_home = Path(environ.get("XDG_DATA_HOME") or home / ".local/share")
    data_dirs = [Path(each) for each in (environ.get("XDG_DATA_DIRS") or "/usr/local/share:/usr/share").split(":") if each]
    for directory in [data_home, *data_dirs]:
        path = directory / "applications" / desktop_id
        if not path.is_file():
            continue
        parser = configparser.ConfigParser(interpolation=None, strict=False)
        parser.read(path)
        if parser.has_option("Desktop Entry", "Exec"):
            words = [word for word in shlex.split(parser.get("Desktop Entry", "Exec")) if not word.startswith("%")]
            return words if words and shutil.which(words[0], path=environ.get("PATH")) else None
    return None


def macos_handler(plist: bytes) -> str | None:
    """The bundle id LaunchServices opens https with."""
    for entry in plistlib.loads(plist).get("LSHandlers", []):
        if entry.get("LSHandlerURLScheme") == "https":
            bundle = entry.get("LSHandlerRoleAll")
            return str(bundle) if bundle else None
    return None


def find(explicit: str | None, environ: Mapping[str, str], home: Path, platform: str) -> Browser:
    """*explicit* (a program), else the user's default browser, else the first of `FALLBACK` on PATH."""
    if explicit is not None:
        family = family_of(Path(explicit).name)
        if family is None:
            raise BrowserError(f"{explicit} is neither a Chromium nor a Firefox family browser")
        return Browser(family, (explicit,), explicit)
    if platform == "darwin":
        plist = home / LAUNCH_SERVICES
        bundle = macos_handler(plist.read_bytes()) if plist.is_file() else None
        if bundle is not None and (family := family_of(bundle)) is not None:
            return Browser(family, ("open", "-n", "-W", "-b", bundle, "--args"), bundle)
    else:
        for desktop in https_handlers(mimeapps_lists(environ, home)):
            argv = desktop_exec(desktop, environ, home)
            if argv is not None and (family := family_of(desktop) or family_of(argv[0])):
                return Browser(family, tuple(argv), desktop)
    for name in FALLBACK:
        if (program := shutil.which(name)) is not None and (family := family_of(name)) is not None:
            return Browser(family, (program,), name)
    raise BrowserError("found no Chromium or Firefox family browser; name one with --browser")


# ── launching ──────────────────────────────────────────────────────


def chromium_argv(browser: Browser, profile: Path, proxy: tuple[str, int]) -> list[str]:
    host, port = proxy
    # Chromium resolves names at a SOCKS5 proxy itself: `socks5h` is not its spelling.
    return [
        *browser.argv,
        f"--user-data-dir={profile}",
        f"--proxy-server=socks5://{host}:{port}",
        "--no-first-run",
        "--no-default-browser-check",
    ]


def firefox_prefs(proxy: tuple[str, int]) -> str:
    """A `user.js` for this launch's profile."""
    host, port = proxy
    prefs = {**FIREFOX_PREFS, "network.proxy.socks": host, "network.proxy.socks_port": port}
    # A JSON string, number or boolean is the same literal in user.js.
    return "".join(f"user_pref({json.dumps(name)}, {json.dumps(value)});\n" for name, value in prefs.items())


def firefox_argv(browser: Browser, profile: Path) -> list[str]:
    return [*browser.argv, "-profile", str(profile), "-no-remote", "-new-instance"]


def certutil() -> str:
    """NSS's certutil on PATH, which the `vivarium` CLI's wrapper puts there."""
    program = shutil.which("certutil")
    if program is None:
        raise BrowserError("trusting the lab root needs NSS's certutil on PATH")
    return program


def add_root(database: Path, root: Path, tool: str) -> None:
    """The lab root, trusted for servers, in the NSS database at *database*, with certutil *tool*."""
    database.mkdir(parents=True, exist_ok=True)
    if not (database / "cert9.db").exists():
        subprocess.run([tool, "-N", "--empty-password", "-d", f"sql:{database}"], check=True)
    subprocess.run(
        [tool, "-A", "-n", LAB_ROOT_NICKNAME, "-t", "C,,", "-i", str(root), "-d", f"sql:{database}"], check=True
    )


def prepare(
    browser: Browser,
    home: Path,
    proxy: tuple[str, int],
    root: Path | None,
    platform: str,
    tool: str | None = None,
) -> tuple[list[str], dict[str, str]]:
    """The command and the variables a launch with profile *home* needs; writes the profile.
    *tool* is certutil, from PATH when None."""
    profile = home / "profile"
    profile.mkdir(parents=True, exist_ok=True)
    extra: dict[str, str] = {}
    if browser.family is Family.FIREFOX:
        (profile / "user.js").write_text(firefox_prefs(proxy))
        if root is not None:
            add_root(profile, root, tool or certutil())
        return firefox_argv(browser, profile), extra
    if root is not None and platform != "darwin":
        # Chromium on Linux reads ~/.pki/nssdb only, so this launch gets a home of its own.
        add_root(home / ".pki/nssdb", root, tool or certutil())
        extra["HOME"] = str(home)
    return chromium_argv(browser, profile, proxy), extra


def launch(browser: Browser, exported: Mapping[str, str], out: Path, urls: Sequence[str], platform: str) -> int:
    """Run *browser* into the run in *out* until it exits; its profile goes with it."""
    proxy_url = exported.get(exports.PROXY)
    if proxy_url is None:
        raise BrowserError(f"the run in {out} serves no proxy: its router has no `vivarium.router.socks`")
    proxy = socks_endpoint(proxy_url)
    root = out / access.DIRECTORY / trust.CERTIFICATE
    lab_root = root if root.is_file() else None
    if lab_root is not None and platform == "darwin" and browser.family is Family.CHROMIUM:
        sys.stderr.write(
            f"vivarium browser: Chromium on macOS trusts the keychain only; lab HTTPS needs {root} there once:\n"
            f"  security add-trusted-cert -r trustRoot -k ~/Library/Keychains/login.keychain-db {root}\n"
        )
    with tempfile.TemporaryDirectory(prefix="vivarium-browser-") as home:
        argv, extra = prepare(browser, Path(home), proxy, lab_root, platform)
        try:
            return subprocess.run([*argv, *urls], env=os.environ | extra, check=False).returncode
        except OSError as error:
            raise BrowserError(f"{argv[0]}: {error.strerror}") from None

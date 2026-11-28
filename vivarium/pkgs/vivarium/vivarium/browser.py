"""`vivarium browser`: the user's own browser, into a live run's lab. nixidae/nixidae#1656.

The browser gets a profile of its own for this launch, so it runs beside
the user's open browser and leaves that profile alone. It reaches the lab
through the session's split proxy (`VIVARIUM_PROXY`): SOCKS5 with remote
names, so the lab resolves its own names and the browser resolves none.
It trusts the run's roots (`access/ca.pem`: the lab root, then the test's
`trust.roots`) from a real certificate store, where their name
constraints hold: Firefox's profile, and Chromium's NSS database under a
home of its own on Linux. Not Chromium's
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
PEM_BEGIN: Final = "-----BEGIN CERTIFICATE-----"
PEM_END: Final = "-----END CERTIFICATE-----"

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
    data_dirs = [
        Path(each) for each in (environ.get("XDG_DATA_DIRS") or "/usr/local/share:/usr/share").split(":") if each
    ]
    desktops = [each.lower() for each in environ.get("XDG_CURRENT_DESKTOP", "").split(":") if each]
    directories = [
        config_home,
        *config_dirs,
        data_home / "applications",
        *(each / "applications" for each in data_dirs),
    ]
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
            found += [
                each.strip() for each in parser.get("Default Applications", HTTPS_HANDLER).split(";") if each.strip()
            ]
    return found


def desktop_exec(desktop_id: str, environ: Mapping[str, str], home: Path) -> list[str] | None:
    """The program of *desktop_id*'s `Exec=` line, field codes such as `%U` dropped. None when
    its program is not there, as for `TryExec`: a profile's entry can outlive its store path."""
    data_home = Path(environ.get("XDG_DATA_HOME") or home / ".local/share")
    data_dirs = [
        Path(each) for each in (environ.get("XDG_DATA_DIRS") or "/usr/local/share:/usr/share").split(":") if each
    ]
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


def certificates(pem: str) -> list[str]:
    """Each PEM certificate in *pem*, in order."""
    blocks = pem.split(PEM_END)
    return [block[block.index(PEM_BEGIN) :] + PEM_END + "\n" for block in blocks if PEM_BEGIN in block]


def nickname(index: int) -> str:
    """The lab root's name in the store, then `... 2`, `... 3` for a test's own roots."""
    return LAB_ROOT_NICKNAME if index == 0 else f"{LAB_ROOT_NICKNAME} {index + 1}"


def add_root(database: Path, root: Path, tool: str) -> None:
    """Every root in *root*, trusted for servers, in the NSS database at *database*, with
    certutil *tool*. certutil imports one certificate per `-i`: the first of a file."""
    database.mkdir(parents=True, exist_ok=True)
    if not (database / "cert9.db").exists():
        subprocess.run([tool, "-N", "--empty-password", "-d", f"sql:{database}"], check=True)
    for index, certificate in enumerate(certificates(root.read_text())):
        subprocess.run(
            [tool, "-A", "-n", nickname(index), "-t", "C,,", "-d", f"sql:{database}"],
            input=certificate,
            text=True,
            check=True,
        )


def prepare(
    browser: Browser,
    home: Path,
    proxy: tuple[str, int],
    root: Path | None,
    platform: str,
    tool: str | None = None,
    environ: Mapping[str, str] | None = None,
    user_home: Path | None = None,
) -> tuple[list[str], dict[str, str]]:
    """The command and the variables a launch with profile *home* needs; writes the profile.
    *tool* is certutil, from PATH when None; *environ* and *user_home* are the user's, this
    process's when None."""
    environ = os.environ if environ is None else environ
    user_home = Path.home() if user_home is None else user_home
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
        extra |= own_home(home, profile, environ, user_home)
    return chromium_argv(browser, profile, proxy), extra


def downloads(environ: Mapping[str, str], home: Path) -> Path:
    """The user's download directory: `XDG_DOWNLOAD_DIR` in `user-dirs.dirs`, else ~/Downloads."""
    dirs = Path(environ.get("XDG_CONFIG_HOME") or home / ".config") / "user-dirs.dirs"
    try:
        lines = dirs.read_text().splitlines()
    except OSError:
        return home / "Downloads"
    for line in lines:
        name, _, value = line.partition("=")
        if name.strip() == "XDG_DOWNLOAD_DIR":
            path = value.strip().strip('"').replace("$HOME", str(home))
            if path.startswith("/"):
                return Path(path)
    return home / "Downloads"


def own_home(home: Path, profile: Path, environ: Mapping[str, str], user_home: Path) -> dict[str, str]:
    """The variables that give Chromium *home*, and keep what the user's home holds for it besides
    trust: their configuration and data (fonts, themes), and their download directory. Measured:
    with HOME alone moved and no XDG_CONFIG_HOME, a download lands in *home* and goes with it."""
    target = str(downloads(environ, user_home))
    preferences = {"download": {"default_directory": target}, "savefile": {"default_directory": target}}
    (profile / "Default").mkdir(parents=True, exist_ok=True)
    (profile / "Default" / "Preferences").write_text(json.dumps(preferences))
    return {
        "HOME": str(home),
        "XDG_CONFIG_HOME": environ.get("XDG_CONFIG_HOME") or str(user_home / ".config"),
        "XDG_DATA_HOME": environ.get("XDG_DATA_HOME") or str(user_home / ".local/share"),
    }


def page(url: str) -> str:
    """*url*, or `https://` *url* when it names no scheme: `web.test` is a page, not a search."""
    if "://" in url or url.partition(":")[0] in {"about", "chrome", "data", "file", "view-source"}:
        return url
    return f"https://{url}"


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
            return subprocess.run([*argv, *map(page, urls)], env=os.environ | extra, check=False).returncode
        except OSError as error:
            raise BrowserError(f"{argv[0]}: {error.strerror}") from None

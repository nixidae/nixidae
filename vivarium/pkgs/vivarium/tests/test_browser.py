"""`vivarium browser`: finding the default browser, and the profile a launch gets. nixidae/nixidae#1656."""

from __future__ import annotations

import datetime
import plistlib
import subprocess
from pathlib import Path

import pytest
import vivarium_ca
from vivarium import browser
from vivarium.browser import Browser, BrowserError, Family


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


@pytest.mark.parametrize(
    ("name", "family"),
    [
        ("chromium-browser.desktop", Family.CHROMIUM),
        ("google-chrome.desktop", Family.CHROMIUM),
        ("com.brave.Browser", Family.CHROMIUM),
        ("firefox.desktop", Family.FIREFOX),
        ("org.mozilla.firefox", Family.FIREFOX),
        ("com.apple.Safari", None),
    ],
)
def test_the_family_comes_from_the_name(name: str, family: Family | None) -> None:
    assert browser.family_of(name) is family


def test_the_proxy_is_its_socks_endpoint() -> None:
    assert browser.socks_endpoint("socks5h://127.0.0.1:41080") == ("127.0.0.1", 41080)
    with pytest.raises(BrowserError, match="not a SOCKS5 address"):
        browser.socks_endpoint("http://127.0.0.1:8080")


def test_a_desktops_own_list_and_the_users_config_come_first(tmp_path: Path) -> None:
    environ = {
        "XDG_CONFIG_HOME": str(tmp_path / "config"),
        "XDG_DATA_DIRS": str(tmp_path / "share"),
        "XDG_CURRENT_DESKTOP": "GNOME",
    }
    lists = browser.mimeapps_lists(environ, tmp_path)
    assert lists[:2] == [tmp_path / "config/gnome-mimeapps.list", tmp_path / "config/mimeapps.list"]
    _write(tmp_path / "share/applications/mimeapps.list", "[Default Applications]\nx-scheme-handler/https=firefox.desktop\n")
    assert browser.https_handlers(lists) == ["firefox.desktop"]
    _write(tmp_path / "config/mimeapps.list", "[Default Applications]\nx-scheme-handler/https=;chromium.desktop;\n")
    assert browser.https_handlers(lists) == ["chromium.desktop", "firefox.desktop"]


def _program(path: Path) -> Path:
    _write(path, "#!/bin/sh\n")
    path.chmod(0o755)
    return path


def test_the_program_is_the_exec_line_without_field_codes(tmp_path: Path) -> None:
    chromium = _program(tmp_path / "bin/chromium")
    _write(
        tmp_path / "share/applications/chromium.desktop",
        f"[Desktop Entry]\nName=Chromium\nExec={chromium} --enable-thing %U\n",
    )
    environ = {"XDG_DATA_DIRS": str(tmp_path / "share"), "XDG_DATA_HOME": str(tmp_path / "none")}
    assert browser.desktop_exec("chromium.desktop", environ, tmp_path) == [str(chromium), "--enable-thing"]
    assert browser.desktop_exec("absent.desktop", environ, tmp_path) is None


def test_an_entry_whose_program_is_gone_gives_way_to_the_next(tmp_path: Path) -> None:
    """A Firefox entry naming a garbage-collected store path, then a Chromium that is there."""
    chromium = _program(tmp_path / "bin/chromium")
    _write(tmp_path / "share/applications/firefox.desktop", "[Desktop Entry]\nExec=/nix/store/gone-firefox/bin/firefox %U\n")
    _write(tmp_path / "share/applications/chromium.desktop", f"[Desktop Entry]\nExec={chromium} %U\n")
    _write(
        tmp_path / "config/mimeapps.list",
        "[Default Applications]\nx-scheme-handler/https=firefox.desktop;chromium.desktop\n",
    )
    environ = {
        "XDG_CONFIG_HOME": str(tmp_path / "config"),
        "XDG_DATA_DIRS": str(tmp_path / "share"),
        "XDG_DATA_HOME": str(tmp_path / "none"),
    }
    assert browser.find(None, environ, tmp_path, "linux") == Browser(Family.CHROMIUM, (str(chromium),), "chromium.desktop")


def test_macos_reads_the_https_handler_from_launch_services(tmp_path: Path) -> None:
    plist = plistlib.dumps(
        {
            "LSHandlers": [
                {"LSHandlerURLScheme": "mailto", "LSHandlerRoleAll": "com.apple.mail"},
                {"LSHandlerURLScheme": "https", "LSHandlerRoleAll": "org.mozilla.firefox"},
            ]
        },
        fmt=plistlib.FMT_BINARY,
    )
    assert browser.macos_handler(plist) == "org.mozilla.firefox"
    _write(tmp_path / browser.LAUNCH_SERVICES, "")
    (tmp_path / browser.LAUNCH_SERVICES).write_bytes(plist)
    found = browser.find(None, {}, tmp_path, "darwin")
    assert found == Browser(Family.FIREFOX, ("open", "-n", "-W", "-b", "org.mozilla.firefox", "--args"), "org.mozilla.firefox")


def test_a_named_program_of_neither_family_is_refused() -> None:
    with pytest.raises(BrowserError, match="neither a Chromium nor a Firefox"):
        browser.find("/usr/bin/safari", {}, Path("/"), "linux")


def test_chromium_reaches_the_proxy_with_remote_names(tmp_path: Path) -> None:
    argv = browser.chromium_argv(Browser(Family.CHROMIUM, ("chromium",), "chromium"), tmp_path, ("127.0.0.1", 1080))
    assert "--proxy-server=socks5://127.0.0.1:1080" in argv
    assert f"--user-data-dir={tmp_path}" in argv


def test_firefox_prefs_send_names_to_the_proxy() -> None:
    prefs = browser.firefox_prefs(("127.0.0.1", 1080))
    assert 'user_pref("network.proxy.socks", "127.0.0.1");' in prefs
    assert 'user_pref("network.proxy.socks_port", 1080);' in prefs
    assert 'user_pref("network.proxy.socks_remote_dns", true);' in prefs


def _root(tmp_path: Path) -> Path:
    root = vivarium_ca.authority.root("vivarium test root", datetime.datetime.now(datetime.UTC))
    path = tmp_path / "ca.pem"
    path.write_text(root.certificate_pem())
    return path


def _trusted(database: Path) -> str:
    return subprocess.run(
        [browser.certutil(), "-L", "-d", f"sql:{database}"], capture_output=True, text=True, check=True
    ).stdout


def test_firefox_gets_prefs_and_the_lab_root_in_its_profile(tmp_path: Path) -> None:
    home = tmp_path / "home"
    firefox = Browser(Family.FIREFOX, ("firefox",), "firefox")
    argv, extra = browser.prepare(firefox, home, ("127.0.0.1", 1080), _root(tmp_path), "linux")
    assert argv == ["firefox", "-profile", str(home / "profile"), "-no-remote", "-new-instance"]
    assert extra == {}
    assert "socks_remote_dns" in (home / "profile/user.js").read_text()
    assert browser.LAB_ROOT_NICKNAME in _trusted(home / "profile")


def test_chromium_on_linux_gets_a_home_with_the_lab_root(tmp_path: Path) -> None:
    home = tmp_path / "home"
    chromium = Browser(Family.CHROMIUM, ("chromium",), "chromium")
    _, extra = browser.prepare(chromium, home, ("127.0.0.1", 1080), _root(tmp_path), "linux")
    assert extra == {"HOME": str(home)}
    assert browser.LAB_ROOT_NICKNAME in _trusted(home / ".pki/nssdb")


def test_chromium_on_macos_leaves_its_home_alone(tmp_path: Path) -> None:
    chromium = Browser(Family.CHROMIUM, ("open", "-n", "-W", "-b", "com.google.Chrome", "--args"), "com.google.Chrome")
    _, extra = browser.prepare(chromium, tmp_path / "home", ("127.0.0.1", 1080), _root(tmp_path), "darwin")
    assert extra == {}
    assert not (tmp_path / "home/.pki").exists()

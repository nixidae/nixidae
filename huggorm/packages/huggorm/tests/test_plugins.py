"""`init_plugins` loads what `plugin-files` names, once per process.

A second call loads nothing and says so, so two callers in one process
need no guard of their own.

Each case runs in a child, because loading a plugin changes the process
for good: the store types it registers cannot be taken back.
`HUGGORM_TCP_STORE_PLUGINS` is the lane's `nix-tcp-store`, which the
package build and `nix run --file . test` both set.
"""

import os
import subprocess
import sys

from nixversion import NO_STORE_SCHEME

PLUGINS = os.environ["HUGGORM_TCP_STORE_PLUGINS"]

CHILD = """
import json, sys
import huggorm_bindings as h

def attempt(action):
    try:
        action()
        print("ok")
    except Exception as e:
        print(type(e).__name__, e)

if sys.argv[1] == "load":
    h.set_setting("plugin-files", sys.argv[2])
    print(h.init_plugins())
print("TCP Daemon Store" in json.loads(h.store_types_json()))
attempt(lambda: h.Store("tcp://127.0.0.1:1").is_valid_path(
    h.StorePath("7rjjfrn5w3z1kb2v9v0ilxmvmb2n5k1y-hello-2.12.1")))
print(h.init_plugins())
"""


def run(*args: str) -> list[str]:
    done = subprocess.run(
        [sys.executable, "-c", CHILD, *args], capture_output=True, text=True, timeout=60)
    assert done.returncode == 0, (done.returncode, done.stderr[-2000:])
    return done.stdout.splitlines()


def test_a_loaded_plugin_registers_its_store_and_connects() -> None:
    first, registered, connect, second = run("load", PLUGINS)
    assert first == "True"
    assert registered == "True"
    # The plugin's own message: the store opened, and its connection
    # code ran inside this process.
    assert "cannot connect to the Nix daemon at '127.0.0.1:1'" in connect, connect
    assert second == "False"


def test_without_init_plugins_the_scheme_is_unknown() -> None:
    registered, connect, first = run("none")
    assert first == "True"
    assert registered == "False"
    assert f"{NO_STORE_SCHEME} 'tcp'" in connect, connect

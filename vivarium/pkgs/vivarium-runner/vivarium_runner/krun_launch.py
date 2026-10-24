"""``python -m vivarium_runner.krun_launch SPEC``: one libkrun guest, in this process.

libkrun is a library, and ``krun_start_enter`` takes the calling process
over for good: it returns only on an error, and the guest's poweroff ends
the process. So the backend runs this module as the guest's process, with
the fds it needs inherited, and *SPEC* (JSON, :class:`KrunSpec`) names them.

The kernel's console is a serial port on fds 0 and 1, which the runner
reads the way it reads QEMU's ``-serial stdio``.
"""

from __future__ import annotations

import ctypes
import json
import os
import sys
from dataclasses import dataclass
from typing import Final

DISK_FORMAT_QCOW2: Final = 1
KERNEL_FORMAT_ELF: Final = 1
SEMANTICS_LINUX_COMPLETE: Final = 0
AGENT_PORT: Final = b"vivarium-agent"
LOG_LEVEL_ENV: Final = "VIVARIUM_KRUN_LOG_LEVEL"
PR_SET_NAME: Final = 15
PROCESS_NAME: Final = b"vivarium-krun"

NET_FEATURE_GUEST_CSUM: Final = 1 << 1
NET_FEATURE_GUEST_TSO4: Final = 1 << 7
NET_FEATURE_GUEST_TSO6: Final = 1 << 8

NET_FEATURES: Final = NET_FEATURE_GUEST_CSUM | NET_FEATURE_GUEST_TSO4 | NET_FEATURE_GUEST_TSO6
"""Receive offloads only.

libkrun advertises neither ``VIRTIO_NET_F_MTU`` nor mergeable receive
buffers, so without a ``GUEST_TSO`` feature the driver posts 1518-byte
receive buffers and drops every frame above that: passt's 65520 and a
segment's 65000. With one, it posts 64 KiB "big packets". No transmit
offload: the virtio-net header does not cross a segment or passt, so a
guest must send whole frames with their checksums done."""


@dataclass(frozen=True, slots=True)
class Nic:
    fd: int
    mac: str


@dataclass(frozen=True, slots=True)
class KrunSpec:
    libkrun: str
    cpus: int
    memory_mib: int
    vmlinux: str
    initrd: str
    cmdline: str
    nested: bool
    disk: str
    store: str
    dax_window: int
    agent_fd: int
    uplink: Nic
    lan: tuple[Nic, ...]
    artifacts: str | None

    @classmethod
    def from_json(cls, data: dict) -> KrunSpec:
        return cls(
            libkrun=data["libkrun"],
            cpus=data["cpus"],
            memory_mib=data["memory_mib"],
            vmlinux=data["vmlinux"],
            initrd=data["initrd"],
            cmdline=data["cmdline"],
            nested=data["nested"],
            disk=data["disk"],
            store=data["store"],
            dax_window=data["dax_window"],
            agent_fd=data["agent_fd"],
            uplink=Nic(**data["uplink"]),
            lan=tuple(Nic(**nic) for nic in data["lan"]),
            artifacts=data["artifacts"],
        )


def _mac(text: str) -> ctypes.Array[ctypes.c_uint8]:
    return (ctypes.c_uint8 * 6)(*(int(octet, 16) for octet in text.split(":")))


def _bind(lib: ctypes.CDLL) -> None:
    """Argument types for every call: ctypes passes a bare int as a C int,
    which truncates the DAX window's ``uint64_t``."""
    u32, i32, u64 = ctypes.c_uint32, ctypes.c_int32, ctypes.c_uint64
    cstr, boolean = ctypes.c_char_p, ctypes.c_bool
    mac = ctypes.POINTER(ctypes.c_uint8)
    signatures = {
        "krun_set_log_level": [u32],
        "krun_create_ctx": [],
        "krun_set_vm_config": [u32, ctypes.c_uint8, u32],
        "krun_disable_implicit_console": [u32],
        "krun_disable_implicit_vsock": [u32],
        "krun_set_nested_virt": [u32, boolean],
        "krun_set_kernel": [u32, cstr, u32, cstr, cstr],
        "krun_add_serial_console_default": [u32, ctypes.c_int, ctypes.c_int],
        "krun_add_virtio_console_multiport": [u32],
        "krun_add_console_port_inout": [u32, u32, cstr, ctypes.c_int, ctypes.c_int],
        "krun_add_virtiofs4": [u32, cstr, cstr, u64, boolean, u32],
        "krun_add_disk2": [u32, cstr, cstr, u32, boolean],
        "krun_add_net_unixstream": [u32, cstr, ctypes.c_int, mac, u32, u32],
        "krun_add_net_unixgram": [u32, cstr, ctypes.c_int, mac, u32, u32],
        "krun_start_enter": [u32],
    }
    for name, argtypes in signatures.items():
        function = getattr(lib, name)
        function.argtypes = argtypes
        function.restype = i32


class KrunError(Exception):
    pass


def run(spec: KrunSpec) -> None:
    # A name of its own, not `python`, so a guest is found by name in
    # `top` and in tests/cleanup.py. libkrun's threads inherit it.
    ctypes.CDLL(None, use_errno=True).prctl(PR_SET_NAME, PROCESS_NAME, 0, 0, 0)
    lib = ctypes.CDLL(spec.libkrun)
    _bind(lib)

    def call(name: str, *args: object) -> int:
        result = getattr(lib, name)(*args)
        if result < 0:
            raise KrunError(f"{name}: {os.strerror(-result)} ({result})")
        return result

    enc = str.encode
    # Errors only by default: libkrun logs each guest request at its own
    # default. 0 off, 1 error, 2 warn, 3 info, 4 debug, 5 trace.
    call("krun_set_log_level", int(os.environ.get(LOG_LEVEL_ENV, "1")))
    ctx = call("krun_create_ctx")
    call("krun_set_vm_config", ctx, spec.cpus, spec.memory_mib)
    call("krun_disable_implicit_console", ctx)
    call("krun_disable_implicit_vsock", ctx)
    call("krun_set_nested_virt", ctx, spec.nested)
    call(
        "krun_set_kernel",
        ctx,
        enc(spec.vmlinux),
        KERNEL_FORMAT_ELF,
        enc(spec.initrd),
        enc(spec.cmdline),
    )

    # The runner's stdin is /dev/null, and libkrun stops the guest when
    # the serial port's input reaches end of file (measured). A pipe whose
    # write end this process holds never does.
    console_in, keep_open = os.pipe()
    os.set_inheritable(keep_open, False)
    call("krun_add_serial_console_default", ctx, console_in, 1)

    # The agent's channel. A port that is not a console never becomes
    # hvc0; udev links it as /dev/virtio-ports/<name>, which
    # modules/qemu.nix names.
    console = call("krun_add_virtio_console_multiport", ctx)
    call(
        "krun_add_console_port_inout",
        ctx,
        console,
        AGENT_PORT,
        spec.agent_fd,
        spec.agent_fd,
    )

    call(
        "krun_add_virtiofs4",
        ctx,
        b"nix",
        enc(spec.store),
        spec.dax_window,
        True,
        SEMANTICS_LINUX_COMPLETE,
    )
    if spec.artifacts is not None:
        call(
            "krun_add_virtiofs4",
            ctx,
            b"artifacts",
            enc(spec.artifacts),
            0,
            False,
            SEMANTICS_LINUX_COMPLETE,
        )
    call("krun_add_disk2", ctx, b"root", enc(spec.disk), DISK_FORMAT_QCOW2, False)

    call(
        "krun_add_net_unixstream",
        ctx,
        None,
        spec.uplink.fd,
        _mac(spec.uplink.mac),
        NET_FEATURES,
        0,
    )
    for nic in spec.lan:
        call("krun_add_net_unixgram", ctx, None, nic.fd, _mac(nic.mac), NET_FEATURES, 0)

    call("krun_start_enter", ctx)


def main() -> int:
    if len(sys.argv) != 2:
        print("usage: python -m vivarium_runner.krun_launch SPEC", file=sys.stderr)
        return 2
    try:
        run(KrunSpec.from_json(json.loads(sys.argv[1])))
    except KrunError as error:
        print(f"krun: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

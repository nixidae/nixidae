"""The container backend: a guest under crun, with no kernel of its own."""

from __future__ import annotations

import json
import os
import pwd
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from .. import container, forward
from ..store import ContainerStore
from .base import BackendError, Launch, Stop, _path, launch_config
from .host import memory_bytes, processes_pss_kib, socket_dir

if TYPE_CHECKING:
    from ..boot import Boot
    from ..machine import Machine
    from ..machine_spec import MachineSpec
    from ..store import Store


@dataclass(frozen=True)
class ContainerConfig:
    """`vivarium.launch.config` under the container backend."""

    crun: Path
    setpriv: Path
    passt: Path
    """passt's own path; pasta is the same program, beside it."""

    @classmethod
    def of(cls, spec: MachineSpec) -> ContainerConfig:
        config = launch_config(spec, "container", frozenset({"crun", "setpriv", "passt"}))
        return cls(crun=_path(config, "crun"), setpriv=_path(config, "setpriv"), passt=_path(config, "passt"))


class Container:
    """A guest as a rootless container: no kernel boot, the host's own.

    Needs what :func:`vivarium_runner.container.probe` checks, and says which of
    it is missing before anything starts. The uplink and the forwards are
    pasta's, joined to the guest's namespaces by the launcher. Each segment
    interface is a tap relayed to its segment fd, so a segment mixes all
    three kinds.
    No memory control yet.
    """

    name = "container"
    reboots = False
    has_kernel = False

    def __init__(self) -> None:
        # One per process, as `available` makes it: the probes run once a run.
        self.host = container.Host()

    @staticmethod
    def validate(spec: MachineSpec) -> None:
        ContainerConfig.of(spec)
        Container._init(spec)

    @staticmethod
    def _init(spec: MachineSpec) -> str:
        if spec.boot is None or spec.boot.init is None:
            raise BackendError(f"{spec.name}: a container guest needs vivarium.boot.system")
        if spec.boot.kernel is not None or spec.boot.firmware is not None:
            raise BackendError(f"{spec.name}: a container guest boots no kernel and no firmware")
        return spec.boot.init

    @staticmethod
    def with_kernel(spec: MachineSpec, kernel: Path) -> MachineSpec:
        raise BackendError(f"{spec.name}: a container guest runs on the host's kernel, so there is none to replace")

    @staticmethod
    def rebooted(stop: Stop) -> bool:
        return False

    @staticmethod
    def host_memory_kib(spec: MachineSpec, pid: int) -> int:
        # Its own cgroup's count where it has one; the processes' PSS
        # where it has none, which is every uid-range build.
        own = container.own_cgroup_memory_kib(pid)
        return own if own is not None else processes_pss_kib(pid)

    @staticmethod
    def store(rundir: Path, paths: list[str]) -> Store:
        return ContainerStore.build(rundir / "nix", paths, rundir / "crun")

    def launch(
        self,
        machine: Machine,
        rundir: Path,
        agent_fd: int,
        lan_fds: dict[str, int],
        boot: Boot | None = None,
        disk_fds: tuple[int, ...] = (),
    ) -> Launch:
        spec = machine.spec
        config = ContainerConfig.of(spec)
        missing = self.host.missing(tun=bool(lan_fds))
        if missing:
            raise BackendError("this host cannot run a container guest:\n" + "\n".join(f"  {item}" for item in missing))
        rootfs = rundir / "root"
        shutil.copytree(spec.image, rootfs, symlinks=True)
        # The template is in the store, so every copy is read-only.
        for directory, _, _ in rootfs.walk():
            directory.chmod(0o755)
        (rootfs / "tmp").mkdir(exist_ok=True)
        (rootfs / "tmp").chmod(0o1777)

        sockets, cleanup = socket_dir(rundir, "agent/sock", None)
        agent_dir = sockets / "agent"
        agent_dir.mkdir()
        if not container.setuid_allowed(rundir):
            (agent_dir / container.NO_SETUID).touch()

        # pasta's tap and the LAN's are made on /dev/net/tun, which a Nix
        # sandbox has only with /dev/net in `extra-sandbox-paths`. With no
        # tun the guest has no uplink; the sandbox has no network anyway.
        # The uplink is taken where a tap can be made and left out where it
        # cannot; the LAN is required, and probe() checked it above. An
        # uplink segment (a router's, nixidae/nixidae#1404) is a tap like
        # vec1, and replaces pasta.
        pasta = spec.uplink is None and (bool(lan_fds) or self.host.tap_failure is None)

        uid, gid = os.getuid(), os.getgid()
        subuid: container.Range | None = None
        subgid: container.Range | None = None
        if not container.owns_ids():
            user = pwd.getpwuid(uid).pw_name
            subuid = container.subordinate(Path("/etc/subuid"), user, uid)
            subgid = container.subordinate(Path("/etc/subgid"), user, uid)
            if subuid is None or subgid is None:
                raise BackendError(f"{spec.name}: {user} has no subordinate ids in /etc/subuid and /etc/subgid")

        bundle = rundir / "bundle"
        bundle.mkdir()
        (bundle / "config.json").write_text(
            json.dumps(
                container.oci_config(
                    hostname=spec.name,
                    init=Container._init(spec),
                    setpriv=str(config.setpriv),
                    rootfs=rootfs,
                    store=spec.store,
                    agent_dir=agent_dir,
                    artifacts=machine.artifacts,
                    uid=uid,
                    gid=gid,
                    subuid=subuid,
                    subgid=subgid,
                    writable_store=container.store_is_one_mount(spec.store),
                    store_view=spec.store_paths is not None,
                    kvm=spec.nested,
                )
            )
        )
        state = rundir / "crun"
        state.mkdir()
        lan: list[str] = []
        if spec.uplink is not None:
            lan += ["--lan", f"{lan_fds['vec0']},vec0,{spec.mac(0)},{spec.mtu}"]
        for nic in spec.interfaces:
            lan += ["--lan", f"{lan_fds[nic.name]},{nic.name},{nic.mac},{nic.mtu}"]
        # A delegated scope of its own where the user's systemd makes one:
        # a cgroup the guest's systemd writes in, its memory counted apart
        # from every other guest's, and `vivarium.memory` as its limit.
        # Otherwise the cgroup this runs in, which is every uid-range build.
        prefix = self.host.scope(memory_bytes(spec.memory)) or []
        return Launch(
            argv=[
                *prefix,
                sys.executable,
                "-m",
                "vivarium_runner.crun_launch",
                "run",
                "--crun",
                str(config.crun),
                "--state",
                str(state),
                "--bundle",
                str(bundle),
                "--name",
                f"guest-{spec.name}-{os.getpid()}",
                *lan,
                # The uplink: pasta joins the guest's namespaces once crun
                # has an init, with passt's arguments and forwards.
                *(
                    [
                        "--pasta-log",
                        str(rundir / "passt.log"),
                        "--",
                        str(config.passt.with_name("pasta")),
                        "--foreground",
                        "--ns-ifname",
                        "vec0",
                        *forward.uplink_args(machine.spec.uplink_network, machine.offline),
                        *self._pasta_forwards(forward.to_args(machine.forward)),
                    ]
                    if pasta
                    else []
                ),
            ],
            pass_fds=tuple(lan_fds.values()),
            agent_path=agent_dir / "sock",
            cleanup=cleanup,
            notes=forward.uplink_notes(machine.spec.uplink_network, machine.offline) if pasta else [],
        )

    @staticmethod
    def _pasta_forwards(args: list[str]) -> list[str]:
        """*args*, plus what makes pasta forward only what passt would.

        pasta forwards more by default: UDP ports it scans for, and the
        guest's loopback to the host's. passt does neither, so a guest
        that could reach the host's loopback here could not under UML.
        """
        extra = ["--tcp-ns", "none", "--udp-ns", "none"]
        for flag in ("--tcp-ports", "--udp-ports"):
            if flag not in args:
                extra += [flag, "none"]
        return [*args, *extra]

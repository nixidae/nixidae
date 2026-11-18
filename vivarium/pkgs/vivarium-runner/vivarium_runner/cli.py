"""``vivarium-run``: boot one guest interactively, or run one command in it.

The NixOS side generates a wrapper with every path already filled in
(see ``system.build.vivariumRunner``), so from a shell this is::

    nix run .#speedtest
    result/bin/vivarium-run --command 'systemctl status'
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import TYPE_CHECKING

import anyio

from .control import ControlSpec
from .forward import ForwardError, Rule, UplinkNetwork
from .machine import LaunchSpec, Machine, MachineError, MachineSpec
from .spec import BackendName, Seccomp, UplinkProxy

if TYPE_CHECKING:
    from anyio.abc import TaskGroup

_WATCH_INTERVAL = 1.0
"""How often to ask the guest what it is listening on: the rate pasta
polls ``/proc/net/tcp`` at for its own forwarding."""


def _parse(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Boot a NixOS system under UML")
    parser.add_argument("--kernel", type=Path, required=True)
    parser.add_argument("--root-image", type=Path, required=True)
    parser.add_argument("--bridge", type=Path, required=True)
    parser.add_argument("--passt", type=Path, required=True)
    parser.add_argument("--ssh-port", type=int, required=True)
    parser.add_argument(
        "--forward",
        required=True,
        help="JSON list of forward rules, as vivarium.forward generates",
    )
    parser.add_argument(
        "--uplink-network",
        required=True,
        help="JSON of the uplink's addresses, as nix/uplink.nix writes them",
    )
    parser.add_argument("--mem", required=True)
    parser.add_argument("--mtu", type=int, required=True)
    parser.add_argument(
        "--command",
        help="run this in the guest, print its output, and power off",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=600,
        help="seconds to allow --command (default: %(default)s)",
    )
    return parser.parse_args(argv)


def _report(machine: Machine, ports: list[int]) -> None:
    """Say what the guest is listening on, and where that answers."""
    if not ports:
        return
    print("[vivarium] listening in guest:", file=sys.stderr, flush=True)
    for port in ports:
        where = machine.reachable(port)
        print(
            f"[vivarium]     {port:<6}-> {', '.join(where) if where else 'not forwarded'}",
            file=sys.stderr,
            flush=True,
        )


async def _watch(machine: Machine) -> None:
    """Report the guest's listening ports as they come and go.

    Only run when there is no ``--command``: the agent serves one
    request at a time, so polling would otherwise take turns with
    whatever the caller actually wanted to do.
    """
    seen: list[int] = []
    while True:
        try:
            ports = await machine.listening()
        except (MachineError, OSError, EOFError):
            return
        if ports != seen:
            _report(machine, [port for port in ports if port not in seen])
            seen = ports
        await anyio.sleep(_WATCH_INTERVAL)


async def _run(args: argparse.Namespace) -> int:
    async with anyio.create_task_group() as tasks:
        code = await _drive(args, tasks)
        tasks.cancel_scope.cancel()
    return code


async def _drive(args: argparse.Namespace, tasks: TaskGroup) -> int:
    machine = Machine(
        MachineSpec(
            name="uml",
            backend=BackendName.UML,
            kind="nixos",
            index=0,
            image=args.root_image,
            memory=args.mem,
            seccomp=Seccomp.AUTO,
            cpus=1,
            ssh_port=args.ssh_port,
            mtu=args.mtu,
            network=None,
            address=None,
            control=ControlSpec(),
            interfaces=(),
            uplink=None,
            uplink_proxy=UplinkProxy.PASST,
            uplink_network=UplinkNetwork.from_json(json.loads(args.uplink_network)),
            router=False,
            forward=tuple(Rule.from_json(rule) for rule in json.loads(args.forward)),
            launch=LaunchSpec(
                "uml", {"kernel": str(args.kernel), "bridge": str(args.bridge), "passt": str(args.passt)}
            ),
        ),
        tasks=tasks,
        on_console=lambda name, line: print(f"[{name}] {line}", file=sys.stderr, flush=True),
    )
    machine.resolve_forward(set())
    await machine.start()
    try:
        if args.command:
            done = await machine.execute(args.command, timeout=args.timeout)
            if done.stderr:
                print(done.stderr, file=sys.stderr, flush=True)
            print(done.stdout, flush=True)
            return done.returncode
        # No command: leave it running and stream the console until the
        # guest exits or the user interrupts us.
        where = machine.reachable(args.ssh_port)
        if where:
            address, _, port = where[0].rpartition(":")
            print(f"[vivarium] up; ssh -p {port} root@{address}, ^C to stop", file=sys.stderr, flush=True)
        else:
            print(
                f"[vivarium] up; guest port {args.ssh_port} is not forwarded, ^C to stop",
                file=sys.stderr,
                flush=True,
            )
        async with anyio.create_task_group() as group:
            group.start_soon(_watch, machine)
            result = await machine.wait(timeout=None)
            group.cancel_scope.cancel()
        return result
    finally:
        await machine.shutdown()


def main(argv: list[str] | None = None) -> None:
    try:
        raise SystemExit(anyio.run(_run, _parse(argv)))
    except KeyboardInterrupt:
        raise SystemExit(130) from None
    except BaseExceptionGroup as group:
        # A task group may wrap a guest's error; unwrap the first one that
        # is ours, so the message stays what it was.
        match, _ = group.split((MachineError, ForwardError))
        if match is None:
            raise
        print(f"[vivarium] {match.exceptions[0]}", file=sys.stderr, flush=True)
        raise SystemExit(1) from None
    except (MachineError, ForwardError) as error:
        print(f"[vivarium] {error}", file=sys.stderr, flush=True)
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()

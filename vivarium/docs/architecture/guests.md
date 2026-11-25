# Guests: boot, segments and the uplink

A guest is one `Machine` (`vivarium_runner/machine.py`). `Machine` is
the same for every backend. A backend answers one question: which
program to start, with which file descriptors, and which helper
processes beside it (`Backend` in `vivarium_runner/backends/base.py`).

## Boot

```{graphviz} boot.dot
```

**Prepare.** `Session.prepare` makes a `Run` (`vivarium_runner/run.py`)
and calls `Run.prepare`. No guest boots yet.

1. `build_lans` makes one `Lan` per segment (`vivarium_runner/net.py`).
2. `Session._machine` makes a `Machine` for each guest. The backend
   checks the guest's `vivarium.launch` here, so a bad configuration
   stops the run before any guest boots.
3. `Machine.resolve_forward` binds the host port of each
   `vivarium.inbound` entry (`forward.claim`). A taken fixed port fails
   here, with the guest's name.
4. `Run.prepare` tells the router which guests it serves
   (`Machine.routed`) and tells each of those guests its router
   (`Machine.behind`).
5. `Session.prepare` writes `access.json`, starts a SOCKS proxy on the
   host for a router with `socks.enable`, and gives `vms` its settings,
   knobs and the other run values.

**Boot.** `Session.ensure` calls `Run.ensure` with the guests a phase
needs. `required` adds each guest that they name in `vivarium.requires`,
transitively. `_start_all` starts them all at once, and lets each one
finish even when another fails, so the teardown finds no half-started
guest. When two phases ask for one guest, the second waits for the
first boot.

`Machine.start` then:

1. makes the guest's view of its closure (`Backend.store`): a tmpfs with
   one read-only bind mount per store path (`vivarium_runner/storeview.py`),
   or the form that the backend needs;
2. asks the backend for a `Launch`: the argv, the file descriptors to
   pass, and the helpers. The egress is one of the helpers
   (`start_egress` in `backends/host.py`);
3. starts the process (`GuestProcess.spawn`) and reads its console
   (`ConsoleLines`);
4. waits for the line `vivarium-agent: ready` on the console, within
   `boot_timeout`. A guest with another `vivarium.control` waits for
   its own line, or for nothing;
5. connects arpyc over the agent socket (`connect` in
   `vivarium_agent/arpyc.py`) and starts `Machine._watch`, which notices
   when the guest stops by itself.

The guest is then in `Run.up`, the `Machines` object that phases and
`exec` see. A guest that is still booting is not in it.

**Instances.** `Session.launch` adds a guest from a template while the
run goes. `Run.add` takes the lowest free index (`free_index`), makes
the instance's spec (`instance_spec` in `vivarium_runner/instances.py`)
and plugs it into its segments with `Lan.add_member`. Each of those
segments is a hub. `Session.terminate` powers the instance off and
frees its index. `Run.add` does not do steps 3 and 4 of the
preparation. So the template's own `vivarium.inbound` is never bound,
and an instance behind the router has no working host forward at all
(nixidae/nixidae#1609).

**Teardown.** `Run.teardown` calls `Machine.shutdown` for each guest
whose boot began. The agent is asked to power the guest off, and the
guest gets `POWEROFF_GRACE` seconds to exit. Then
`GuestProcess.reap` signals its process group: SIGTERM, 30 seconds,
then SIGKILL. A run with no control socket sends SIGKILL at once.

## The agent

A NixOS guest runs `vivarium-agent` (`vivarium_agent/agent.py`), a
systemd unit from the package `vivarium-agent`. A guest holds that
package and not the runner. The runner and the agent speak arpyc (`vivarium_agent/arpyc.py`): rpyc's
wire format over asyncio, with one handler that calls a method by name.
So `await vm.succeed(...)` is one round trip.

The agent's channel is one end of a socketpair that `Machine._boot`
makes. The backend passes the other end into the guest: UML as the
serial line `ssl0`, QEMU as a chardev socket, krun with `--agent-fd`.
The container backend has no such device: the agent listens on a Unix
socket that the host binds in (`Launch.agent_path`). The channel needs
no network, so a command works before the guest's network is up and
inside a build sandbox.

`Machine._ask` is the one place where a call to the agent is timed and
recorded. `report.json` reads from it.

## Segments

A segment is a layer 2 link between interfaces of guests. An interface
names its segment in `vivarium.interfaces.<name>.segment`.

```{graphviz} network.dot
```

- **Two members:** the two ends of one `SOCK_SEQPACKET` socketpair. The
  frames do not pass through the runner.
- **Three or more members, or `hub = true`:** a `Lan` in the runner
  floods each frame to every other port. Only a hub can `cut`,
  `restore` and `capture`, and only a hub takes a member while the run
  goes, which is why a template's segments are hubs.

Every backend takes the same file descriptor, so one segment can join
guests of different backends.

## The uplink

`vec0` is the uplink: the guest's way to the host's network, and the
way in for `vivarium.inbound`. `vivarium.uplink.proxy` chooses what
serves it.

**`vsock`**, the default for a NixOS guest. The guest runs the relay in
a network namespace of its own (`ownRelay` in `modules/router.nix`).
`vec0` is one end of a veth pair. nftables tproxy hands each flow to the
relay, and the relay carries it over vsock to the guest's own egress on
the host. The egress opens the flow on the host's network. A container
guest uses two Unix sockets in place of vsock.

**`router`**, when the run has a router guest (`router.enable`).
`routerModule` in `lib.nix` puts the guest's `vec0` on the segment
`uplink-<name>`, and the router has a bridge port `g<index>` on that
segment. The router leases the guest `10.0.2.(15 + index)` by DHCP. The
router runs the relay and has the only egress. Every instance of a
template shares the template's segment. When a template sits behind the
router, the router has a lease for each index below 200 that no fixed
guest holds, so an instance needs no change on the router.

**Inbound.** The runner binds each forward's host port itself
(`forward.claim`). It passes the bound socket to the egress over the
egress's control socket (`egress_exchange` in `backends/host.py`). The
egress accepts on it and opens the flow through the relay to the guest.
For a routed guest, the router's egress holds the forwards of every
guest it serves (`egress_adds`). `Machine.open_inbound` adds a forward
while the guest runs.

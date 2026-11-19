# Router lab

Explore mode, on bookmark `explore/router`: this work may not land. It
lands only as a good implementation; otherwise the plan in
nixidae/nixidae#1404 is reconsidered.

## Goal

Every multi-node run has one router guest: UML or libkrun on Linux,
libkrun on macOS. Guests have internal networks only, and reach the
outside through the router. The router's own Linux kernel does the work
of passt and gvproxy. Target: some Gbit/s on dynhetz, unloaded.

## Shape under test

- Guests to router: a direct segment per guest (socketpair).
- In the router: nftables. TCP: `redirect` (NAT), so conntrack tracks
  the ports, and the relay reads `SO_ORIGINAL_DST`. UDP: `tproxy`, which
  is not NAT, so the relay keeps its own flow table and reads
  `IP_ORIGDSTADDR`. ICMP echo: a fwmark and a `local` route to a raw
  socket (E5).
- Relay to host: vsock. libkrun has vsock natively. UML gets it from
  `virtio_uml` and a vhost-user vsock device that the host process
  serves itself (E7).
- Host: one process per run opens the real TCP, UDP and ping sockets,
  and listens for forwards (E6).
- Rust for the relay and the host process. The lab's libkrun launcher
  (`krun-router.c`) is C, like `pkgs/krun-launch`: libkrun's C API. A
  Rust launcher would link libkrun's crates directly.

## Questions, cheapest and most decisive first

| id | question | result |
| --- | --- | --- |
| E1 | vsock throughput, UML guest to host, through `virtio_uml` and `vhost-device-vsock` | 1.9-3.8 Gbit/s at 64-256 KiB writes, with `vsock-linear-tx.patch`. Below |
| E2 | E4's path with a libkrun router (KVM) | works; at MTU 65000 16.2-16.7/10.0-10.3 Gbit/s, passt on krun 4.8-8.3/13.2-18.1. Below |
| E3 | smallest router: kernel plus initramfs, host RSS and boot time | UML: 36 MiB, 1.0 s (E1). krun: 119 MiB at 256 MiB guest RAM, no balloon in the lab launcher; 1.2-1.5 s (E2) |
| E4 | TCP through the whole path: guest, router NAT, relay, vsock, host | works; 3.4/5.1 Gbit/s at MTU 1500 (passt 1.7/2.7), 5.1/1.4 at MTU 65000 (passt 18.3/16.6). Below |
| E5 | UDP and ICMP echo through the path | works on krun: ping, UDP echo, iperf3 `-u` both ways; negative control holds. Below |
| E6 | a forward: host port to a guest's server through the router | works on krun: 9.2 Gbit/s into the guest, 14.7 out; on UML (E7) 12.3 and 15.3. Below |
| E7 | our own vhost-user vsock device in the egress, UML router on the stock kernel | works; one flow 13.2-15.3/10.9-12.7 Gbit/s at MTU 65000. Below |
| E8 | what limits the leg into the guest | the guest's 256 KiB vsock window, not libkrun's muxer; on UML, wakeup preemption, which the relay's yield fixes. Below |
| E10 | the router as a NixOS guest | UML: 6.4 s and 129 MiB slimmed, against 1.0 s and 36 MiB for busybox. Below |
| E11 | one VRF per router network, a relay bound to each (nixidae/nixidae#1523) | works; no leak through the router. On macOS the host's shared 127.0.0.1 still joins two networks. Below |

## E1, 2026-11-16

`lab/router/e1.py`: the router initrd streams 4 GiB of zeros with
`socat` to host vsock port 5000. dynhetz, 16 cores, host load 3-37 (other
work); numbers are from runs back to back.

- Boot to first byte: 0.97-1.05 s. Peak RSS of the UML kernel process:
  36 MiB at `mem=128M`.
- 8 KiB writes: 2.04 Gbit/s. The same pipe to `/dev/null` in the guest:
  2.1 Gbit/s. The guest's system calls are the limit, not vsock.
- 64 KiB writes stall with no byte delivered: `DescriptorLengthTooSmall`.
  Linux 7.2 sends a packet above about 32 KiB as a nonlinear skb, one
  descriptor per fragment. rust-vmm's virtio-vsock (0.10 in the release,
  0.12 on main) reads data from one descriptor only.
- With `vsock-linear-tx.patch` (packets kept linear): 1.9-3.8 Gbit/s at
  64-256 KiB writes. CPU for 4 GiB: guest 5 s, backend 2 s, against 9-18 s
  wall. Both sides wait more than they work: the window or the
  notifications, not CPU, set the rate.
- `tx-buffer-size=1048576` stalls at every write size. Not followed up.

So vhost-user vsock on UML is fast enough for the goal, but not with
rust-vmm's backend as it stands. Our own Rust backend that reads any
descriptor chain is the next step if E4 holds up.

## E4, 2026-11-16

`lab/router/e4.py`: a client UML guest and the router UML guest on one
vec socketpair. The router redirects TCP to `relay` (Rust, static),
which reads `SO_ORIGINAL_DST` and opens one vsock stream per flow to
`egress` (Rust) on the host, which connects to the destination. The
host's iperf3 listens on 127.0.0.1; the guest reaches it as 10.0.2.2.
Kernel with `vsock-linear-tx.patch`, vhost-device-vsock 0.3.0, iperf3
`-l 128K`, 10 s, host load 10-17.

The baseline is the same UML guest with its own passt
(`lab/router/passt-uml.nix`, the `checks.uplink-iperf` phase).

| MTU | path | guest to host | host to guest |
| --- | --- | --- | --- |
| 1500 | router | 3.39 Gbit/s | 5.06 Gbit/s |
| 1500 | passt | 1.73 Gbit/s | 2.66 Gbit/s |
| 65000 | router | 5.08 Gbit/s | 1.35 Gbit/s (74 retransmits) |
| 65000 | passt | 18.29 Gbit/s | 16.62 Gbit/s |

- At MTU 1500 the router path is about twice passt: the guest's TCP
  ends in the router's kernel, and from there the data moves in large
  vsock packets, not 1500-byte frames.
- At MTU 65000 passt is 3-12 times faster. vsock caps the router path at
  about 5 Gbit/s, as in E1. vivarium's default MTU is 65000, so this is
  the comparison that counts.
- The host-to-guest drop at MTU 65000 has retransmits. Not explained
  yet.

CPU seconds over the 10 s run at MTU 65000 (`e4.py` prints them; host
load 12-15). This pair reached 6.11 and 4.18 Gbit/s, so the 1.35 above
is run-to-run noise, not a property of the direction.

| direction | client | router | vhost-device-vsock | egress | iperf3 |
| --- | --- | --- | --- | --- | --- |
| guest to host | 3.6 | 6.1 | 3.4 | 2.1 | 1.1 |
| host to guest | 6.0 | 10.1 | 12.6 | 1.0 | 0.5 |

- Guest to host: nothing is saturated, so round trips set the rate.
- Host to guest: the router is at one full CPU, and UML is
  uniprocessor. The router guest is the limit.

## E2, 2026-11-16

`lab/router/e2.py`: E4's path, with the router and the client under
libkrun (`lab/router/krun-router.c`, 2 vCPUs, 256 MiB each). The router's
vsock is libkrun's own device: a guest connection to CID 2, port 5000
becomes a connection to the egress's unix socket. No vhost-user backend,
no linear-tx patch: libkrun reads any descriptor chain. The kernel is the
stock NixOS 6.18.55 with its modules in the initrd. Host load 4-7.

The baseline is a krun guest with its own passt
(`LAB_BACKEND=krun lab/router/passt-uml.nix`, `--cpus 2`). A krun
guest's uplink stays at MTU 1500 whatever `vivarium.mtu` says
(nixidae/nixidae#1179), so the lab node sets `vec0`'s MTU itself.

| MTU | path | guest to host | host to guest |
| --- | --- | --- | --- |
| 1500 | krun router | 5.98 Gbit/s | 5.76 Gbit/s |
| 1500 | krun passt | 8.86 Gbit/s | 6.24 Gbit/s |
| 65000 | krun router | 16.2-16.7 Gbit/s | 10.0-10.3 Gbit/s |
| 65000 | krun passt | 4.8-8.3 Gbit/s | 13.2-18.1 Gbit/s |

- At MTU 65000 the router path is in passt's range: about twice passt
  out of the guest, about two thirds of passt into it. On UML the router
  path was 3-12 times slower than passt.
- The passt baseline serves from the guest through a forward; the router
  path connects out of the guest. Same bytes, other direction of setup.
- The router boots to "router ready" in 1.2-1.5 s.

A UML client (vivarium's default backend) behind the krun router, same
runs:

| MTU | guest to host | host to guest |
| --- | --- | --- |
| 65000 | 14.7 Gbit/s | 10.4 Gbit/s |
| 1500 | 6.29 Gbit/s | 3.94 Gbit/s |

Against UML with its own passt (E4): 18.3/16.6 at 65000, 1.7/2.7 at 1500.

Router vCPUs at MTU 65000 (`E2_CPUS`). "Busy" is the router's own
`/proc/stat` (`e2.py` prints it), over about 12 s of samples that hold
the 10 s run:

| vCPUs | guest to host | host to guest | router busy |
| --- | --- | --- | --- |
| 1 | 11.4-12.0 Gbit/s | 7.0-7.1 Gbit/s | 79-82% of 1 |
| 2 | 16.1-16.7 Gbit/s | 9.8-10.1 Gbit/s | 60% of 2 |
| 4 | 18.3-19.4 Gbit/s | 8.9-9.1 Gbit/s | 41% of 4 |

- On the host every vCPU thread reads near 100% under load, also with
  4. That is KVM's halt polling (`halt_poll_ns` 200000 here): the guest
  is idle for part of that time. Idle, the router uses 0.02 s per 10 s.
- With 1 vCPU the router is the limit. With 2 or more it is not.
- Host to guest stays near 10 Gbit/s while the guest has idle CPU. The
  limit is in libkrun: its `vsock muxer` thread (4-6 s per run) and its
  `virtio-net work` thread are one thread each.

So the shape holds on krun: some Gbit/s with room to spare, and one
vCPU already beats the UML router.

Without `/dev/kvm` (a Nix sandbox without KVM, CI runners without it)
the router is UML, and E4 holds: at MTU 65000 it is 3-12 times slower
than passt. A router design that replaces passt everywhere must either
accept that, keep passt for runs without KVM, or make the UML path
faster (our own vhost-user vsock backend, E1).

## E5, 2026-11-16

`lab/router/e5.py`: the krun client and router of E2, MTU 65000. The
host runs a UDP echo on 127.0.0.1:7777 and `iperf3 -s`; the guest
reaches both as 10.0.2.2.

How each protocol reaches the relay (`router.nft`, `relay.rs`):

- UDP: nftables `tproxy` to the relay's port 9001, with mark 1. A fwmark
  rule and a `local` route deliver the marked datagram to the router.
  tproxy is not NAT, so no conntrack entry holds the flow. This departs
  from "conntrack tracks the ports": the relay keeps its own flow table
  and ends a flow after 30 s with no datagram. Per flow, the relay binds
  a transparent socket to the original destination and connects it to
  the guest. The guest's later datagrams arrive on that socket, and
  replies leave from it.
- ICMP echo: mark 1 and the same route, no NFQUEUE. A raw socket in the
  relay reads each echo request, original destination included. The
  router's kernel answers none (`icmp_echo_ignore_all`). The host sends
  the echo through a ping socket, which picks its own echo id; the relay
  puts the guest's id back and injects the reply with an `IPPROTO_RAW`
  socket.
- On vsock, a UDP or ICMP flow is one stream of datagrams, each with a
  2-byte length. A datagram that meets a full stream is dropped, as on a
  congested link.
- The host's `ping_group_range` here is `0 2147483647`, so the egress
  needs no privilege for ICMP. macOS allows ping sockets to every user.

Results:

- `ping 10.0.2.2`: 3 of 3, 0.5-0.8 ms.
- `ping 192.0.2.1` (TEST-NET-1, nothing answers): 0 of 2. The router
  makes up no replies.
- `ping` to the host's own public address: 2 of 2. Negative control:
  with the mangle chain flushed (`noicmp=1`), 0 of 2, and the UDP echo
  gets nothing. A ping to 10.0.2.2 still answers then, because that
  address is the router's own and its raw socket sees the echo anyway.
- UDP echo: the reply comes back.

iperf3 `-u`, 5 s, host load 6-7:

| rate, datagram | guest to host: received, loss | host to guest: received, loss |
| --- | --- | --- |
| 600 Mbit/s, 1400 B | 594 Mbit/s, 0.12% | 600 Mbit/s, 0.8% |
| 1 Gbit/s, 1400 B | 786 Mbit/s, 21% | 930 Mbit/s, 7.7% |
| 2 Gbit/s, 8000 B | 1.97 Gbit/s, 0.51% | 1.99 Gbit/s, 1.1% |
| 5 Gbit/s, 32000 B | 4.29 Gbit/s, 14% | 4.99 Gbit/s, 1.1% |

- The limit is datagrams per second, about 70-80 thousand: each one is
  a system call at every hop, and a vsock frame of its own.
- Not measured yet: where the loss happens (the relay's drop on a full
  stream, or a socket buffer), and E5 on the UML router.

## E6, 2026-11-16

`lab/router/e6.py`: a forward, as `vivarium.inbound` does with passt.
The egress listens on the host's 127.0.0.1:5202. Per connection it
connects to a unix socket that libkrun listens on (`krun-router` with
`IN_PORT IN_PATH`, `add_unix_port(..., listen = true)`), names the guest
address, and the relay in the router connects to 10.0.2.15:5201. The
guest runs `iperf3 -s`; the host's iperf3 connects. MTU 65000, host load
8-10.

| direction | rate |
| --- | --- |
| host to guest | 9.23 Gbit/s |
| guest to host (`-R`) | 14.7 Gbit/s |

- passt's forward on a krun guest (E2's baseline) was 13.2-18.1 into
  the guest and 4.8-8.3 out of it, under lower load.
- The guest sees the connection come from the router's 10.0.2.1, not
  from the host's address.
- On UML, the egress's own vsock device opens the forward's connection
  (E7): `HOST_PORT=GUEST:PORT@vsock:5001`.

## E7, 2026-11-16

`egress/src/vsock.rs`: the egress serves the UML router's vhost-user
vsock device itself (`egress vhost-user:SOCKET`), with rust-vmm's
vhost-user-backend and virtio-queue for the transport. It reads a packet
from any descriptor chain, so the router runs `kernelStock`, without
`vsock-linear-tx.patch`, and no vhost-device-vsock runs. Each guest
connection is one end of a socketpair; the egress's flow thread holds
the other end. `e4.py ... -`, `e6.py uml ...` and `e7.py`.

E4 again, MTU 65000, iperf3 `-l 128K`, host load 4-10, runs back to
back:

| path | guest to host | host to guest |
| --- | --- | --- |
| UML router, own device, stock kernel | 13.2-15.3 Gbit/s | 10.9-12.7 Gbit/s |
| UML router, vhost-device-vsock, patched kernel (E4) | 5.0 Gbit/s | 4.8 Gbit/s |
| krun router (E2) | 16.2-16.7 Gbit/s | 9.8-10.3 Gbit/s |

- The proof that the stock kernel's chains arrive: 80% of TX chains had
  more than two descriptors (289k of 362k), and every byte arrived.
- Negative control: the same stock kernel with vhost-device-vsock
  delivers 0 bytes in 10 s.
- CPU seconds per 10 s run, guest to host: client 6.5, router 9.0,
  egress 7.0, iperf3 1.8. The router is near one full CPU, and UML is
  uniprocessor.
- E6 on this router: 12.3 Gbit/s into the guest, 15.3 out.
- The egress in unix-socket mode on krun (E2): 16.3 and 9.76, as before.
- Content: `e7.py` sends 169 MB of `seq` output each way through the
  device and compares MD5s. Both match, with 2.5k multi-descriptor
  chains. With the host's receiver stalled 5 s, the device spilled
  219 KB and both still match.
- Eight flows: 14.6-16.0 Gbit/s out of the guest, but 1.2-1.6 into it.
  2 flows: 3.4; 4 flows: 5.5-5.8. vhost-device-vsock with the patched
  kernel gave 3.8 with eight flows (E4), so the own device is worse
  here. The router uses a full CPU for 1.6 Gbit/s. The guest sends a
  credit update for almost every 4 KiB packet it gets (409k for 504k),
  against one per 43 with one flow.
- Not the cause, each tried with eight flows into the guest:
  - `net.unix.max_dgram_qlen` 512 or 8192: same result.
  - Dropped frames: 26 retransmits.
  - The device's interleaving: bursts of 128 KiB per flow give the
    same ratio and rate.
  - The device's refill: a flow that joins the send queue only with
    half the guest's window free gives the same ratio and rate.

  So the guest's relay reads per packet. E8 explains why.

## E8, 2026-11-16

The leg into the guest. The router's `/init` prints each CPU's
`/proc/stat` row, its virtio interrupt rows and every busy thread;
`e2.py` splits each libkrun vCPU thread into guest time, userspace exits
and KVM. MTU 65000, iperf3 `-l 128K`, one flow unless noted.

libkrun's vsock muxer is not the limit on the krun router:

- The muxer thread uses 4.0 s of a 10 s run at 9.8 Gbit/s.
- Four router vCPUs give 8.8-9.0 Gbit/s, two give 9.9-10.2.
- libkrun handles exits in userspace for 0.4-0.7 s per vCPU.
- The guest's CPU1 is about 90% busy: the relay (4.6 s), the
  `virtio_vsock` kworkers (about 2 s) and hard IRQ (2.6 s).

The guest's vsock receive window is a limit. Linux gives a vsock socket
256 KiB, and that is the credit the guest gives the host. The relay now
sets 4 MiB (`vsockbuf=` on the router's kernel line). Pairs back to
back, host load about 10:

| path | 256 KiB | 4 MiB |
| --- | --- | --- |
| krun, host to guest | 7.7-9.6 Gbit/s | 10.9-12.1 Gbit/s |
| krun, guest to host | 13.8-14.3 Gbit/s | 18.1-18.6 Gbit/s |
| krun, eight flows into the guest | 11.6 Gbit/s | 13.3 Gbit/s |
| UML, host to guest | 9.7 Gbit/s | 13.1 Gbit/s |

16 MiB gives no more than 4 MiB. The krun router does not collapse with
eight flows; with them its muxer thread uses 9.5 s of 10.

The collapse into a UML router's guest (E7) is the guest's scheduler.
UML is uniprocessor. Each packet the `virtio_vsock` kworker queues wakes
a relay thread, and the relay thread preempts the kworker. It reads the
one packet, empties the socket, and Linux then sends a credit update. A
relay under `SCHED_BATCH` does not preempt the thread that woke it
(`batch=1` on the router's kernel line):

| UML router, 4 MiB window | default | `SCHED_BATCH` |
| --- | --- | --- |
| eight flows into the guest | 1.23-1.33 Gbit/s | 10.1-10.7 Gbit/s |
| credit updates per packet, eight flows | 0.80-0.87 | 0.03 |
| one flow into the guest | 13.1 Gbit/s | 13.9 Gbit/s |
| one flow out of the guest | 14.3-15.7 Gbit/s | 12.6-13.8 Gbit/s |
| eight flows out of the guest | 10.3 Gbit/s | 12.0 Gbit/s |

- The cost: about 12% on one flow out of the guest.
- One run into the guest with `SCHED_BATCH` gave 3.2 Gbit/s, against
  7.4 for its partner at host load 13. It is not explained.
- Latency, `lab=latency`: the client pings the host through the relay,
  idle and then beside the flows into the guest. Host load 24-40, two
  rounds:

  | eight flows into the guest | Gbit/s | busy ping avg / max |
  | --- | --- | --- |
  | default | 1.04, 0.91 | 11.4 / 36 ms, 13.2 / 84 ms |
  | `SCHED_BATCH` | 6.77, 0.90 | 2.7 / 12 ms, 40.8 / 264 ms |

  Idle pings average 1.1-2.5 ms in all runs. Under this load
  `SCHED_BATCH` collapsed once anyway, with worse latency than the
  default. So it is not a fix.
- Nice 19 for the relay lifts eight flows only from 1.0 to 1.4-1.5
  Gbit/s. UML has no lazy preemption (`ARCH_HAS_PREEMPT_LAZY`).
- The fix: the relay calls `sched_yield()` before a read when its last
  read was under 64 KiB. The kworker then queues the rest of its batch
  first, and the read takes many packets. A yield before every read
  cost one flow into the guest 15-30%. `yield=0` turns it off. Pairs
  back to back, host load 7-12, two rounds:

  | UML router | no yield | yield after a short read |
  | --- | --- | --- |
  | one flow into the guest | 13.0, 14.8 Gbit/s | 13.4, 14.8 Gbit/s |
  | eight flows into the guest | 1.30, 1.15 Gbit/s | 7.6, 9.1 Gbit/s |
  | one flow out of the guest | 16.6, 15.0 Gbit/s | 15.5, 16.4 Gbit/s |
  | eight flows out of the guest | 12.6, 7.5 Gbit/s | 12.6, 8.1 Gbit/s |
  | busy ping avg, eight flows in | 4.6, 6.7 ms | 2.2, 1.5 ms |

  On the krun router at host load 14-18 it costs nothing: one flow in
  8.9-9.9 Gbit/s against 5.9-7.8, out 16.9-18.2 against 14.4-17.3,
  eight flows in 8.4-14.1 per second against 10.7-13.1.
- The lab's per-thread listing (`tasks=1`) is opt-in: on UML the walk
  of `/proc` costs `init` 1.7-3.1 s of CPU in a 10 s run.

## E10, 2026-11-16

Can the router be a NixOS guest, a vivarium machine like any other?
`lab/router/nixos`: three UML guests at 256 MiB boot together. `plain`
is a default vivarium guest. `slim` drops the Nix database, Nix, oomd,
timesyncd and audit. `slimmer` also drops sshd, resolved, nscd and the
setuid wrappers. `plain` and `slimmer` share a segment and ping each
other. Boot is to the agent's "ready"; host memory is
`host_memory_kib()` after `drop_caches`. Host load 3-6.

| guest | boot | host memory | guest used | running units |
| --- | --- | --- | --- | --- |
| `plain` | 7.6 s | 155 MiB | 81 MiB | 27 |
| `slim` | 7.1 s | 145 MiB | 68 MiB | 24 |
| `slimmer` | 6.4 s | 129 MiB | 64 MiB | |
| busybox router (E1, E3) | 1.0 s | 36 MiB | | |

- udev stays. Without it `slimmer` boots in 6.0 s at 115 MiB, but
  networkd leaves every link "pending": it waits for udev, which also
  renames the segment interfaces. The agent's device unit needs it too.

- The kernel takes 1.5 s of each NixOS boot; systemd reaches `-.mount`
  at 1.9 s.
- In `slimmer` the agent is the largest process: 35 MiB RSS (Python).
  systemd is 14 MiB, networkd 9 MiB.
- Each guest's `vec0` today has the host's IPv6 prefix from passt
  (`2a01:...`), besides 10.0.2.15. A router replaces that.
- krun: two runs failed in the guests at host load 35 ("decompression
  failed with status 3", resolved and networkd failed to start). Not
  followed up.

## Slice 1, 2026-11-16

The UML router as a vivarium machine: `router.enable` in `mkTest`,
`modules/router.nix`, the egress in the runner. `checks.router` runs
two guests behind one router and passes sandboxed.

- Each guest leases 10.0.2.(15 + index) from the router by its MAC.
- Forwards: the egress binds each routed guest's `vivarium.inbound`,
  TCP and UDP, on the address and port `forward.reachable` names. A
  guest with every port takes 72,555 sockets, ready in 0.26 s, 7.7 MiB.
- As passt: a wide rule skips a port it cannot bind; the privileged
  ports move up by 10000; the ephemeral range is left out.
- Not as passt: the privileged run is weak too. This host holds
  `*:10250` (a kubelet), and passt fails the guest on it.
- DNS: 10.0.2.3:53 maps to `VIVARIUM_DNS_HOST`, else 1.1.1.1, over UDP
  and TCP. Offline: the egress binds outward sockets to 127.0.0.1, so
  DNS and the outside fail at once and the gateway answers.
- ICMP echo to the outside goes through a ping socket in the egress.
- IPv6: the router advertises `fd00:10:0:2::/64`; each guest takes
  `::(15 + index)` by a networkd token. `fd00:10:0:2::2` is the host's
  `::1`. Online, a guest pings 2606:4700:4700::1111 at 1.7 ms and gets
  HTTP and DNS from it. DNS stays on 10.0.2.3.
- Not as passt: passt gives the guest the host's own global prefix, so
  glibc prefers IPv6. A ULA source makes glibc prefer IPv4
  (`getent ahosts one.one.one.one` lists 1.1.1.1 first).

Traps found:

- systemd-networkd deletes an `ip rule` it does not manage. The relay
  added the fwmark rule by hand, so UDP and ICMP to the outside never
  left the router. The sandbox has no outside and hid it. The uplink's
  `.network` file declares it now, and `checks.router` reads it.
- `ping_group_range` is per network namespace, and a write takes only
  group ids the writer's user namespace maps (`0 2147483647` gives
  EINVAL). An offline run sets it to its own group.
- The build sandbox refuses ping sockets. `checks.router` skips the
  echo check there and says so.
- Another run's passt can hold a port on 127.0.0.x. The host then
  reaches that run's guest. A wide rule avoids an address that has a
  listener (`forward.bound_on`).
- nft tproxy finds an established socket first: a guest's UDP reply
  reaches the relay's connected socket, not the :9001 listener.

Throughput, `lab/router/slice-iperf.nix`: one run, guest `passt` with
its own passt and guest `routed` behind the slice's UML router, MTU
65000, one flow, 5 s, two rounds interleaved per direction. Host load
went from 65 to 12 during the run. "Forward" is the host's client to the
guest's forward; "outbound" is the guest's client to 10.0.2.2.

| path | direction | passt | router |
| --- | --- | --- | --- |
| forward | host to guest | 22.3, 21.1 Gbit/s | 17.9, 17.6 Gbit/s |
| forward | guest to host | 20.8, 21.0 Gbit/s | 20.6, 20.3 Gbit/s |
| outbound | guest to host | 20.9, 20.9 Gbit/s | 19.8, 20.1 Gbit/s |
| outbound | host to guest | 21.3, 16.2 Gbit/s | 17.8, 17.4 Gbit/s |

- Out of the guest the router is within 5% of passt; into the guest,
  about 83% (UML guests). This is above E7's one flow (13.2-15.3 and 10.9-12.7
  Gbit/s). The cause is not known; the host load differed.

krun guests behind the UML router: `vivarium-krun --uplink-segment`
puts vec0 on the router's segment, and `checks.router.krun` passes.
QEMU guests use QEMU's segment uplink; a container guest's vec0 is a
tap relayed to the segment. `checks.router` passes on all four backends.
The same lab with `LAB_BACKEND=krun` (both guests krun, the router UML,
host load 18-38):

| path | direction | passt | router |
| --- | --- | --- | --- |
| forward | host to guest | 14.3, 23.0 Gbit/s | 14.6, 13.3 Gbit/s |
| forward | guest to host | 3.4, 8.6 Gbit/s | 16.9, 16.5 Gbit/s |
| outbound | guest to host | 8.8, 5.5 Gbit/s | 17.1, 10.2 Gbit/s |
| outbound | host to guest | 20.4, 20.9 Gbit/s | 13.3, 14.3 Gbit/s |

- Out of a krun guest the router is about twice passt; into it, about
  65%, as on UML.
- Before nixidae/nixidae#1179 was fixed, a krun guest's vec0 stayed at
  MTU 1500, and the router path out of it gave 1.3-1.9 Gbit/s.

### The router on krun, 2026-11-17

`router.backend = "krun"` runs the router under libkrun, 2 vCPUs and
512M. Its vsock is libkrun's own, on the egress's unix sockets, as in E2
and E6. `checks.router.krun`, `lan-router.krun` and
`forward-router.krun` pass sandboxed. With 16 vCPUs, the router ran
out of memory at 256M during boot. With 2 vCPUs, 256M still
OOM-killed the agent.

Both guests krun, `LAB_ROUTER` alternating krun and UML, two runs each,
two rounds a run, host load 6-7:

| path | direction | krun router | UML router | passt |
| --- | --- | --- | --- | --- |
| forward | host to guest | 8.3-8.5 Gbit/s | 15.9-16.4 Gbit/s | 14.3-26.4 Gbit/s |
| forward | guest to host | 16.7-19.0 Gbit/s | 18.3-19.0 Gbit/s | 5.6-10.6 Gbit/s |
| outbound | guest to host | 16.4-18.5 Gbit/s | 18.0-18.7 Gbit/s | 5.1-11.9 Gbit/s |
| outbound | host to guest | 8.5-8.7 Gbit/s | 15.6-16.5 Gbit/s | 11.9-27.0 Gbit/s |

- Out of the guest the two routers are equal. Into it the krun router
  gives about half of the UML router's rate.
- E2 put the krun router's limit into the guest in libkrun: its vsock
  muxer is one thread. The 4 MiB window (E8) lifted the UML router
  above E7's 11-12, and it does not lift the krun router.
- So the krun router buys nothing on this host.

#### Where the krun router's time goes, 2026-11-17

One iperf3 run of 10 s, host to `routed`, CPU time per thread of the
router's libkrun process: vCPU 0 9.5 s, vCPU 1 9.0 s, vsock muxer 5.7 s,
virtio-net worker 3.9 s. The vCPUs are the limit, not the muxer.

In the guest, cpu0 had 8.3 s of system time, 5.8 s of it the relay's;
cpu1 was 63% idle. Hard interrupts took 4.8 s. Most of them were
virtio-net's TX completions on `g1`: 237k in the run, about 14 us each,
because every one exits to libkrun. The vsock device raised 65k, about
52 packets each.

- The relay on cpu1, beside those interrupts: 9.2 Gbit/s, slower.
- `ethtool -C g1 tx-frames 0` (napi_tx off) frees a sent frame on the
  next send, with no interrupt. `g1`'s interrupts fell to 92k.

napi_tx on against off, one run, three rounds back to back:

| napi_tx | host to guest | guest to host |
| --- | --- | --- |
| on | 8.6-8.7 Gbit/s | 16.6-18.4 Gbit/s |
| off | 12.2-13.7 Gbit/s | 17.5-18.5 Gbit/s |

The router module sets `virtio_net.napi_tx=0` on krun. The slice lab
again, krun and UML router alternating, two runs each, host load 6-7:

| path | direction | krun router | UML router |
| --- | --- | --- | --- |
| forward | host to guest | 13.4-13.6 Gbit/s | 15.0-16.2 Gbit/s |
| forward | guest to host | 17.0-19.7 Gbit/s | 15.8-18.8 Gbit/s |
| outbound | guest to host | 16.8-18.5 Gbit/s | 16.0-18.4 Gbit/s |
| outbound | host to guest | 13.2-14.1 Gbit/s | 13.7-15.9 Gbit/s |

- Into the guest the krun router is now 10-15% below UML, not half.
  Out of it the two are equal.
- libkrun stays unpatched: a batched `recv` in the muxer saves host CPU
  the muxer does not run short of.

### The router on QEMU, 2026-11-17

QEMU's `vhost-user-vsock-pci` device connects to the egress's vhost-user
socket, as UML's kernel does. QEMU negotiates EVENT_IDX; UML does not.

- QEMU gave a guest's vec0 no `host_mtu`, so every QEMU uplink was at
  1500. At 65000, passt out of a QEMU guest went from 5-7 to 16-29
  Gbit/s.
- At 256M the QEMU router stalled into the guest: whole seconds at 0
  Gbit/s, one host retransmit each, no drop or retransmit inside the
  guests. Then the OOM killer took the agent. At 512M, eight runs in a
  row gave 14.9-15.3 Gbit/s with no stall, 202M in use.
- napi_tx off changes nothing on QEMU (14.4-15.4 against 15.1): KVM
  injects the interrupt and nothing exits to the VMM.

QEMU guests, the router on QEMU (512M) and on UML alternating, two runs
each, two rounds a run, host load 7-13. The QEMU router's runs were at
256M; their stalls are the low values into the guest:

| path | direction | QEMU router | UML router | passt |
| --- | --- | --- | --- | --- |
| forward | host to guest | 0.27-15.4 Gbit/s | 14.5-16.2 Gbit/s | 4.5-18.3 Gbit/s |
| forward | guest to host | 26.3-27.6 Gbit/s | 19.0-19.3 Gbit/s | 16.1-25.3 Gbit/s |
| outbound | guest to host | 19.4-28.4 Gbit/s | 17.7-19.2 Gbit/s | 20.1-29.1 Gbit/s |
| outbound | host to guest | 9.7-15.7 Gbit/s | 15.3-16.4 Gbit/s | 11.6-18.0 Gbit/s |

- Out of a guest the QEMU router is the fastest of the three.
- `.qemu` puts the router on QEMU, as `.krun` puts it on krun.

### A guest on vsock, with no router guest, 2026-11-17

`vivarium.uplink.proxy = "vsock"`: the router's ruleset and relay in a
network namespace in the guest, behind a veth pair whose guest end is
`vec0`. The guest has passt's addresses; its egress binds its forwards.
Kernel 7.2 has `net.vsock.ns_mode` and `child_ns_mode`, both `global`, so
connect and listen work from the namespace.

`LAB_ROUTED=vsock`, one run per backend, guest `routed` on vsock against
guest `passt`, two rounds, host load 8-9:

| backend | path | direction | vsock | passt |
| --- | --- | --- | --- | --- |
| UML | forward | host to guest | 15.0-15.2 Gbit/s | 20.3 Gbit/s |
| UML | forward | guest to host | 20.3-21.5 Gbit/s | 19.3-20.6 Gbit/s |
| UML | outbound | guest to host | 19.7-22.7 Gbit/s | 15.0-19.9 Gbit/s |
| UML | outbound | host to guest | 13.8-15.1 Gbit/s | 17.6-19.9 Gbit/s |
| QEMU | forward | host to guest | 15.9-17.2 Gbit/s | 10.7-15.3 Gbit/s |
| QEMU | forward | guest to host | 35.2-39.4 Gbit/s | 3.8-14.6 Gbit/s |
| QEMU | outbound | guest to host | 37.0-38.7 Gbit/s | 15.6-20.0 Gbit/s |
| QEMU | outbound | host to guest | 17.8-18.3 Gbit/s | 16.7-17.6 Gbit/s |
| krun | forward | host to guest | 19.0-19.1 Gbit/s | 5.6-13.0 Gbit/s |
| krun | forward | guest to host | 37.2-37.9 Gbit/s | 8.0-8.3 Gbit/s |
| krun | outbound | guest to host | 35.7-36.8 Gbit/s | 8.1-8.7 Gbit/s |
| krun | outbound | host to guest | 19.0-19.3 Gbit/s | 19.3-19.5 Gbit/s |

- On QEMU and krun the vsock uplink is the fastest measured: out of a
  guest about twice passt on QEMU and four times on krun.
- On UML it is faster out of the guest and slower into it.

## E11, 2026-11-18

`lab/router/vrf.py`, in an unprivileged user and network namespace on
dynhetz (kernel 7.2.4): the namespace is the router. Two networks, a and
b, each a VRF with a bridge, one guest namespace on a veth port, its own
relay and its own egress on unix sockets. The outside (192.0.2.1) is a
namespace of its own behind `out0` in the main table. The relay takes
`RELAY_DEVICE` and binds every IP socket to that device
(`SO_BINDTODEVICE`). Logs: one JSON document per arm.

| probe from guest a | no VRF | VRF, relay bound |
| --- | --- | --- |
| TCP, UDP, ICMP echo to the outside | works | works |
| TCP to its gateway (the host's loopback) | works | works |
| forward from a's relay to guest a | works | works |
| TCP to guest b | reaches b | reset |
| UDP to guest b | reaches b | no answer |
| ICMP echo to guest b | answered | no answer |
| forward from a's relay to guest b | reaches b | refused |
| TCP to its gateway, at b's host forward port on 127.0.0.1 | reaches b | reaches b |

- A VRF runs the prerouting hook a second time, with the VRF as the input
  device. `redirect` matches on the bridge (`iifname "br-a"`) and works.
  `tproxy` on the bridge loses its socket: `UdpNoPorts` counts each
  datagram. `tproxy` on the VRF's pass (`iifname "vrf-a"`) works. One
  `fwmark 1` rule to one table with `local 0.0.0.0/0 dev lo` serves every
  VRF, at priority 500 or 2000 (the VRFs' own rule is at 1000). A rule
  and table per VRF works too. A local route per VRF device in one table
  does not: the second one is refused ("File exists").
- A relay bound to no device, with `tcp`, `udp` and `raw_l3mdev_accept`
  set: its UDP replies and its forwards fail with "Network is
  unreachable", because their sockets route in the main table. Every
  socket of the relay must bind to its VRF. `ip vrf exec` would do the
  same through a cgroup BPF program; not measured, and a container router
  cannot load one.
- The last row is the host, not the router. The lab's forwards bind
  127.0.0.1, and a's egress maps a's gateway to 127.0.0.1, so a reaches
  b's forward. On Linux the runner gives each guest's forwards their own
  127.0.0.x: in `checks.router` by hand, `a` to the gateway at `b`'s port
  got a reset, because `b`'s forward is on 127.0.0.2. On macOS every
  forward shares 127.0.0.1 (`SHARED_LOOPBACK`), and the path is open.
  Only the egress can refuse it.
- Lab traps: `br_netfilter` was loaded on the host, so a new namespace
  starts with `bridge-nf-call-iptables=1`; with it, `tproxy` loses its
  socket even with no VRF. The router guest sets it to 0. An outside
  address local to the router made the relay's per-flow UDP bind fail.
- Not measured here: IPv6, and networkd's DHCP server and router
  advertisements on a bridge inside a VRF. Both need the guest.

## Also measured

- E4 again, with the E5 and E6 initrd and relay: the UML router gives
  5.02 and 4.78 Gbit/s at MTU 65000. E4 holds.
- The default case, back to back, MTU 65000, host load 7-8: a UML guest
  behind the krun router against the same UML guest with its own passt.

  | path | guest to host | host to guest |
  | --- | --- | --- |
  | UML guest, krun router | 14.0-15.4 Gbit/s | 9.0-10.2 Gbit/s |
  | UML guest, own passt | 20.3 Gbit/s | 20.4-20.6 Gbit/s |

  The router path is about 70% of passt out of the guest and half of
  passt into it. Into the guest, the guest's 256 KiB vsock window is a
  limit (E8).
- Eight flows at once (`streams=8`, iperf3 `-P 8`), MTU 65000, host
  load 7-10:

  | router, client | guest to host | host to guest |
  | --- | --- | --- |
  | krun, krun | 14-16 Gbit/s (per-second sums) | 8.9 Gbit/s |
  | UML, UML | 12.3 Gbit/s | 3.8 Gbit/s |

  More flows lift the UML router out of the guest (12.3 against 5 with
  one flow); into the guest it stays at one uniprocessor's limit. Nothing
  stalls.
- The passt baseline's guest-to-host leg failed in 3 of 6 runs here:
  iperf3 got "Transport endpoint is not connected" when it connected
  right after the first leg (nixidae/nixidae#1405).
- `e1.py` and `e4.py` left 13 vhost-device-vsock and 3 UML processes
  behind after stalled runs: `timeout` stops the script before its
  `finally` kills them.

## Open

- ICMP errors back to the guest: UDP to a closed port, or an
  unreachable host, gives the guest nothing.
- A datagram that reaches the UDP listener while its flow's own socket
  is being set up is lost.
- E5 on the UML router: only TCP and the forward are measured there.
- Several flows into a UML router's guest: 7.6-9.1 Gbit/s with the
  relay's yield (E8), against 13-15 for one flow.
- A guest's ping to its gateway 10.0.2.1 gets no reply: the router
  ignores every echo (`icmp_echo_ignore_all`), and the host has no such
  address.
- macOS (2026-11-17, an M-series Mac, krun on HVF): lan-vsock, forward-vsock,
  router, lan-router and forward-router pass. A krun guest boots in about
  7 s. One flow at MTU 65000, iperf3 on the host through a forward
  (`checks.uplink-iperf`, `checks.uplink-iperf-router`):

  | uplink | host to guest | guest to host |
  | --- | --- | --- |
  | gvproxy | 5.22 Gbit/s | 25.71 Gbit/s |
  | vsock | 33.34 Gbit/s | 56.16 Gbit/s |
  | vsock and router | 26.25 Gbit/s | 31.39 Gbit/s |

  Before four libkrun fixes, guest to host through vsock killed or hung
  the VM at the end of each test (`pkgs/libkrun`).

  macOS gives an AF_UNIX stream 8 KiB of buffer each way, and the
  sender's buffer bounds the flow. With that default, vsock carried
  7.91 and 8.51 Gbit/s, and the router 6.83 and 8.00. libkrun and the
  egress now set 1 MiB on their AF_UNIX sockets.
- Not tried: several flows through the slice's router.
- The no-KVM case: without `/dev/kvm` the router is UML. With the own
  device (E7) one flow is about 70% of passt out of the guest and 60%
  into it; several flows into the guest are far below passt.

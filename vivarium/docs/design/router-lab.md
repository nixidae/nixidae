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
  `virtio_uml` and a vhost-user vsock backend.
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
| E6 | a forward: host port to a guest's server through the router | works on krun: 9.2 Gbit/s into the guest, 14.7 out. Below |

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

`lab/router/e6.py`: a forward, as `vivarium.forward` does with passt.
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
- On UML, vhost-device-vsock takes a host connection on its unix socket
  and needs a `CONNECT <port>` line first. The egress does not speak
  that yet.

## Also measured

- E4 again, with the E5 and E6 initrd and relay: the UML router gives
  5.02 and 4.78 Gbit/s at MTU 65000. E4 holds.

## Open

- IPv6: the nftables table is `ip` only.
- ICMP errors back to the guest: UDP to a closed port, or an
  unreachable host, gives the guest nothing.
- A datagram that reaches the UDP listener while its flow's own socket
  is being set up is lost.
- E5 and E6 on the UML router.
- The no-KVM case: without `/dev/kvm` the router is UML, at MTU 65000
  3-12 times slower than passt (E4). The vhost-user vsock backend of E1
  is the lever there.

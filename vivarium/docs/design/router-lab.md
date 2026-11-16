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
- In the router: nftables NAT. `redirect` for TCP and `tproxy` for UDP
  send each flow to a local socket of the relay. conntrack tracks the
  ports; the relay reads the original destination
  (`SO_ORIGINAL_DST`, `IP_ORIGDSTADDR`).
- Relay to host: vsock. libkrun has vsock natively. UML gets it from
  `virtio_uml` and a vhost-user vsock backend.
- Host: one process per run opens the real TCP, UDP and ping sockets.
- Rust for the relay and the host process. C only inside the kernel.

## Questions, cheapest and most decisive first

| id | question | result |
| --- | --- | --- |
| E1 | vsock throughput, UML guest to host, through `virtio_uml` and `vhost-device-vsock` | 1.9-3.8 Gbit/s at 64-256 KiB writes, with `vsock-linear-tx.patch`. Below |
| E2 | E4's path with a libkrun router (KVM) | works; at MTU 65000 16.2-16.7/10.0-10.3 Gbit/s, passt on krun 4.8-8.3/13.2-18.1. Below |
| E3 | smallest UML router: kernel plus initramfs, host RSS and boot time | |
| E4 | TCP through the whole path: guest, router NAT, relay, vsock, host | works; 3.4/5.1 Gbit/s at MTU 1500 (passt 1.7/2.7), 5.1/1.4 at MTU 65000 (passt 18.3/16.6). Below |
| E5 | UDP and ICMP echo through the path | |

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

Router vCPUs at MTU 65000 (`E2_CPUS`):

| vCPUs | guest to host | host to guest |
| --- | --- | --- |
| 1 | 11.9 Gbit/s | 7.0 Gbit/s |
| 2 | 16.7 Gbit/s | 10.1 Gbit/s |
| 4 | 19.4 Gbit/s | 9.1 Gbit/s |

- Every vCPU thread reads near 100% under load, also with 4. KVM's
  `halt_poll_ns` is 200000 here, so a vCPU that waits polls, and its
  CPU time says little. Idle, the router uses 0.02 s per 10 s.
- Guest to host scales with vCPUs. Host to guest does not: libkrun's
  `vsock muxer` thread (4-6 s per run) and the `virtio-net work` thread
  are each one thread.

So the shape holds on krun: some Gbit/s are reached with room to spare,
and one vCPU already beats the UML router.

Next: E5, UDP and ICMP through the same router.

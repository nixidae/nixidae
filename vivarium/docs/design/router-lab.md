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
| E2 | vsock throughput, libkrun guest to host | |
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

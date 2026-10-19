# IPsec site-to-site, strongSwan to strongSwan

A test lab for site-to-site IPsec. Two strongSwan gateways authenticate
with X.509 certificates and connect two sites in two ways: one
policy-based tunnel, and two route-based tunnels with eBGP over them.
The route-based half follows the layout of an AWS Site-to-Site VPN.
Both gateways also have the same subnet behind them, and 1:1 NAT joins
the two over each kind of tunnel.

Each guest is a small NixOS machine under User-Mode Linux, so the whole
lab runs as ordinary processes inside the Nix build sandbox. It needs no
root, no KVM and no cloud account. One run takes about four minutes.

## Topology

```
     alice 10.1.0.10                          bob 10.2.0.10
     carol 10.11.0.10                         dave 10.12.0.10
           |                                        |
      [ west-lan ]                             [ east-lan ]
           |                                        |
         west ------------ [ internet ] ------------ east
     AS65000  |                  |               |  AS64512
     192.0.2.1|               mallory            |  192.0.2.2, .21, .22
              |              192.0.2.66          |
      [ west-nat ]                             [ east-nat ]
              |                                  |
     erin 10.50.0.10                          frank 10.50.0.10
```

| | policy-based | route-based |
| --- | --- | --- |
| Sites | alice's 10.1.0.0/24 and bob's 10.2.0.0/24 | carol's 10.11.0.0/24 and dave's 10.12.0.0/24 |
| east's outside address | 192.0.2.2 | 192.0.2.21 (tunnel 1), 192.0.2.22 (tunnel 2) |
| Traffic selectors | the two /24s | 0.0.0.0/0 on both sides |
| What sends traffic into the tunnel | the kernel's XFRM policy | a route to an XFRM interface, `ipsec1` and `ipsec2` |
| Routing | none | eBGP over each tunnel, 169.254.10.0/30 and 169.254.11.0/30, ECMP over both |
| erin's 10.50.0.0/24 is seen as | 100.64.1.0/24 | 100.64.11.0/24 |
| frank's 10.50.0.0/24 is seen as | 100.64.2.0/24 | 100.64.12.0/24 |
| Tunnel for the mapped ranges | a second CHILD_SA, `netmap`, under the same IKE_SA | the same two tunnels; BGP announces the mapped /24 |

**The colliding subnet.** erin and frank are both 10.50.0.10. Each gateway maps its 10.50.0.0/24 to a range of its own with nftables (`snat ip prefix to`, `dnat ip prefix to`), one range per path. The host part stays the same, so erin reaches frank at 100.64.2.10 or 100.64.12.10, and frank sees erin as 100.64.1.10 or 100.64.11.10. No selector, route or BGP announcement holds 10.50.0.0/24.

**Linux terms, vendor terms:**
- **XFRM policy:** the kernel's crypto map, proxy-ID or encryption domain.
- **XFRM interface** (`if_id`): a route-based tunnel interface, like a VTI on Cisco or a tunnel interface on Palo Alto.
- **CHILD_SA:** the IPsec SA pair, phase 2. **IKE_SA:** phase 1.

**Common to both:**
- **Proposals:** IKEv2 with `aes256gcm16-prfsha384-ecp384`, and ESP with `aes256gcm16-ecp384`.
- **Authentication:** ECDSA P-384 certificates from the lab's own CA, checked against a CRL with `revocation = strict`.
- **Roles:** east plays the AWS end. It accepts a peer from any address, and it does not start the IKE_SA (AWS's default startup action `add`). west plays the customer gateway.
- **Firewall:** nftables drops by default on both gateways.

**Timers.** DPD and BGP are short so that the test can watch them. The AWS column holds the tunnel option defaults.

| Timer | Lab | AWS default |
| --- | --- | --- |
| IKE_SA rekey | 4 h | phase 1 lifetime 8 h (28800 s) |
| CHILD_SA rekey | 1 h | phase 2 lifetime 1 h (3600 s) |
| DPD | every 2 s, dead after about 5.4 s; west restarts, east clears | timeout 30 s, action Clear |
| BGP keepalive / hold | 1 s / 3 s | |

**mallory** is an attacker on the internet. It holds a revoked certificate for west's name and a certificate from a CA that nobody trusts.

## Phases

The phases run in this order. Each phase prints what it measured as `[test]` lines.

| Phase | What it shows | Measured |
| --- | --- | --- |
| `pki` | Each gateway holds its certificate and key, the CA, and a CRL that revokes serial 03. | |
| `policy` | IKE_SA and CHILD_SA use the configured proposals. The kernel holds AES-256-GCM and an out, in and fwd policy for the two /24s. No plaintext ICMP crosses the internet, only ESP. | 288-bit key (256 + salt), 128-bit ICV |
| `route` | Both tunnels come up into XFRM interfaces. eBGP comes up over each, and west installs the far LAN with two nexthops. east also announces a bogus 10.99.0.0/24, and west's prefix list drops it. The two pairs of LANs cannot reach each other. | |
| `netmap` | erin and frank hold the same address. Each reaches the other in both directions, over the `netmap` CHILD_SA without BGP and over the route-based tunnels with BGP. Each sees the other's mapped address, and a TCP service answers with the right host name. The far gateway decrypts on the expected interface, and west's conntrack shows the translation. | Decrypted on `internet` (policy-based) or `ipsecN` (route-based) |
| `mtu` | Path MTU discovery through each kind of tunnel, to the byte. | XFRM interface: 1400. Policy-based: 1446 |
| `pmtud` | DF is copied to the outer header. The sender caches the MTU that the gateway reports. A packet without DF that does not fit is fragmented after encryption on the policy-based tunnel, and before encryption on the route-based one. | Policy-based: 1556 bytes of ESP in outer fragments of 1500 and 76. Route-based: two whole ESP packets |
| `blackhole` | A hop drops ESP over 1400 bytes and sends no ICMP. Ping works and a 4 MB TCP transfer stalls. Clamping the MSS to the route MTU does not help. An MSS of 1300 does. | MSS 1360 stalls, MSS 1300 passes |
| `udp` | UDP has no MSS to clamp. Without DF it is fragmented and arrives whole. With DF the first datagram is lost, and the next send fails locally. | `EMSGSIZE` at the sender |
| `rekey` | CHILD_SA and IKE_SA rekey under a ping every 100 ms. All SPIs change, the new CHILD_SA uses PFS, and BGP stays up. | 60 of 60 pings, ECP_384 |
| `failover` | Tunnel 1 is cut on the wire, with its interfaces still up. BGP moves traffic to tunnel 2, and DPD takes down tunnel 1's IKE_SA. When the path returns, tunnel 1 and ECMP come back without help. | Route moved in 2.5–3.9 s, about 22 of 150 pings lost |
| `impostor` | mallory tries a revoked certificate, a rogue CA, a weak proposal, and plaintext from bob's address. east and west refuse all four. The real IKE_SA does not change. | `AUTH_FAILED`, `AUTH_FAILED`, `NO_PROPOSAL_CHOSEN`, `XfrmInTmplMismatch` 3 |
| `lifecycle` | west loads a renewed certificate and key and reauthenticates under load. Then west loads an expired certificate, and after that east loads a stale CRL. east refuses each reauthentication, but the old IKE_SA keeps traffic flowing until the tunnel is terminated. | 59 or 60 of 60 pings across the renewal |

## Findings

These came out of building the lab. Each one is measured by the phase in brackets, and its comment there has the detail.

1. **A tunnel can stay down after the far end restarts** (`lab.nix`, `policy`). With `start_action = start`, west's first IKE_SA_INIT reached east 80 ms before east had loaded its configuration. east answered `NO_PROPOSAL_CHOSEN`, and charon does not retry after that answer, whatever `keyingtries` says. `start_action = trap|start` fixes it: the trap policy stays, and the next packet for the tunnel starts a new attempt.
2. **Where Linux fragments depends on the kind of tunnel** (`pmtud`). A policy-based SA has no device MTU, so a packet without DF is encrypted first and the ESP packet is fragmented. The far gateway must reassemble before it can decrypt. An XFRM interface with an MTU fragments the inner packet first, and each fragment travels as its own ESP packet. AWS recommends the second, "to avoid decreased performance".
3. **1446 is the inner MTU of a tunnel-mode AES-GCM SA over a 1500 path** (`mtu`). It is also the maximum MTU that AWS publishes for Site-to-Site VPN, with an MSS of 1406.
4. **Set the tunnel interface MTU first, and clamp the MSS second** (`blackhole`, `udp`). A correct interface MTU makes PMTUD work for TCP and UDP. MSS clamping helps only TCP. Clamping to the route MTU does not fix a narrower hop farther away, because the gateway knows only its own interface. If such a hop drops large packets without ICMP, clamp to a fixed value below the real path MTU.
5. **UDP gets through by fragmentation or not at all** (`udp`). An application that sets DF loses the first datagram silently. It must handle `EMSGSIZE` and send smaller.
6. **`unique = never` is needed when several tunnels share one pair of identities** (`lab.nix`, `route`). With the default, INITIAL_CONTACT on one tunnel removes the IKE_SA of the other.
7. **An expired certificate does not take the tunnel down when it expires** (`lifecycle`). With `make_before_break = yes`, a refused reauthentication leaves the old IKE_SA in place, and `swanctl --rekey --reauth` still reports success. The outage comes later, at the next reboot, DPD timeout or terminate. Monitor the expiry date, not the tunnel state. With `make_before_break = no`, the tunnel goes down at the refused reauthentication.
8. **A stale CRL is as bad as an expired certificate** (`lifecycle`). With `revocation = strict`, a CRL past its nextUpdate refuses every peer: `RULE_CRL_VALIDATION is STALE, but requires at least GOOD`. `pki --signcrl` gives a CRL 15 days by default, so somebody must re-sign it on time.
9. **Renewal works without an outage** (`lifecycle`). Load the new certificate and key with `swanctl --load-creds`, then reauthenticate. In 22 tries, 3 reauthentications lost one ping each, and a rekey never lost one.
10. **Overlapping subnets need only NAT on the gateway** (`netmap`). The selectors and BGP use the mapped ranges, and the hosts change nothing. The nftables forward chain sees the inside address, after DNAT and before SNAT. The XFRM out policy matches only after SNAT, when Linux looks the route up again. With a trap policy, that second lookup raises the acquire, and the first packet is lost.
11. **charon installs no route for a selector the gateway has no address in** (`netmap`). For the mapped ranges, table 220 stays empty. The packet goes to the default route, and the firewall drops it. A static route towards the peer fixes it.
12. **Do not look for plaintext leaks in inbound traffic** (`policy`). On a policy-based gateway, tcpdump on the outside interface shows the decrypted replies as inbound plaintext. Only outbound plaintext is a leak.

## Run it

From the root of the vivarium repository (`vivarium/` in nixidae):

```sh
nix build --file . ipsec                     # the whole run, in the sandbox
nix run --file . ipsec.driver -- --out ./out --break failover
```

The build succeeds only if every phase passes, and its log holds the
`[test]` lines. `--break` pauses before the phase you name, with all
guests up. The run prints the full path of the `vivarium` command; use
it in place of `vivarium` below. From another shell you can then look
around:

```sh
vivarium ctl --out ./out exec 'print(await west.succeed("swanctl --list-sas"))'
vivarium ctl --out ./out exec 'print(await west.succeed("ip xfrm state"))'
vivarium ctl --out ./out continue
```

## What this is not

- **One implementation.** Both ends run strongSwan on Linux. There is no Palo Alto, Cisco or AWS endpoint here, only the same layout.
- **A Transit Gateway, not a VGW.** ECMP over both tunnels is what a Transit Gateway does with VPN ECMP turned on. A virtual private gateway accepts traffic on both tunnels, but it sends towards the customer on one of them.
- **No NAT-T.** Both gateways have public addresses, so ESP is not wrapped in UDP 4500. With NAT-T each packet costs 8 bytes more, and 1446 becomes 1438.
- **No traffic selector mismatch.** Each side proposes exactly what the other expects. A real Palo Alto or Cisco peer with other proxy-IDs or crypto ACLs gets `TS_UNACCEPTABLE` or a narrowed CHILD_SA. AWS accepts one SA pair per tunnel.
- **Test keys.** `pki.nix` makes the PKI at build time, so the private keys are in the Nix store. That is correct for a lab and wrong for anything else.

# IPsec site-to-site, strongSwan to strongSwan

A test lab for site-to-site IPsec. Two strongSwan gateways authenticate
with X.509 certificates and connect two sites in two ways: one
policy-based tunnel, and two route-based tunnels with eBGP over them.
The route-based half has the same layout as an AWS Site-to-Site VPN.

Each guest is a small NixOS machine under User-Mode Linux, so the whole
lab runs as ordinary processes inside the Nix build sandbox. It needs no
root, no KVM and no cloud account. One run takes about three minutes.

## Topology

```
            alice 10.1.0.10            bob 10.2.0.10
            carol 10.11.0.10           dave 10.12.0.10
                  |                          |
            [ west-lan ]               [ east-lan ]
                  |                          |
   west  10.1.0.1, 10.11.0.1       east  10.2.0.1, 10.12.0.1
   AS65000                         AS64512
   192.0.2.1 ----- [ internet ] ----- 192.0.2.2   policy-based
                        |        `--- 192.0.2.21  tunnel 1, ipsec1
                     mallory     `--- 192.0.2.22  tunnel 2, ipsec2
                   192.0.2.66
```

| | policy-based | route-based |
| --- | --- | --- |
| Sites | alice's 10.1.0.0/24 and bob's 10.2.0.0/24 | carol's 10.11.0.0/24 and dave's 10.12.0.0/24 |
| Traffic selectors | the two /24s | 0.0.0.0/0 on both sides |
| What sends traffic into the tunnel | the kernel's XFRM policy | a route to an XFRM interface (`if_id` 1 and 2) |
| Routing | none | eBGP over each tunnel, 169.254.10.0/30 and 169.254.11.0/30, ECMP over both |

**Common to both:**
- **Proposals:** IKEv2 with `aes256gcm16-prfsha384-ecp384`, and ESP with `aes256gcm16-ecp384`.
- **Authentication:** ECDSA P-384 certificates from the lab's own CA, checked against a CRL with `revocation = strict`.
- **Roles:** east plays the AWS end. It answers from any address and never initiates. west plays the customer gateway.
- **Firewall:** nftables drops by default on both gateways.

**mallory** is an attacker on the internet. It holds a revoked certificate for west's name and a certificate from a CA that nobody trusts.

## Phases

The phases run in this order. Each phase prints what it measured.

| Phase | What it shows | Measured |
| --- | --- | --- |
| `pki` | Each gateway holds its certificate and key, the CA, and a CRL that revokes serial 03. | |
| `policy` | IKE_SA and CHILD_SA use the configured proposals. The kernel holds AES-256-GCM and an out, in and fwd policy for the two /24s. On the internet there is only ESP. | 288-bit key (256 + salt), 128-bit ICV |
| `route` | Both tunnels come up into XFRM interfaces. eBGP comes up over each, and west installs the far LAN with two nexthops. east also announces a bogus 10.99.0.0/24, and west's prefix list drops it. The two pairs of LANs cannot reach each other. | |
| `mtu` | Path MTU discovery through each kind of tunnel, to the byte. | xfrmi: 1400. Policy-based: 1446 |
| `pmtud` | DF is copied to the outer header. The sender caches the MTU that the gateway reports. A packet without DF is encrypted first and fragmented after. | 1500 bytes inner = 1556 bytes of ESP, in outer fragments of 1500 and 76 bytes |
| `blackhole` | A hop drops ESP over 1400 bytes and sends no ICMP. Ping works and a 4 MB TCP transfer stalls. Clamping the MSS to the route MTU does not help. An MSS of 1300 does. | MSS 1360 stalls, MSS 1300 passes |
| `udp` | UDP has no MSS to clamp. Without DF it is fragmented and arrives whole. With DF the first datagram is lost, and the next send fails locally. | `EMSGSIZE` at the sender |
| `rekey` | CHILD_SA and IKE_SA rekey under a ping every 100 ms. All SPIs change, the new CHILD_SA uses PFS, and BGP stays up. | 60 of 60 pings, ECP_384 |
| `failover` | Tunnel 1 is cut on the wire, with its interfaces still up. BGP moves traffic to tunnel 2, and DPD takes down tunnel 1's IKE_SA. When the path returns, tunnel 1 and ECMP come back without help. | Route moved in 2.5–3.9 s |
| `impostor` | mallory tries a revoked certificate, a rogue CA, a weak proposal, and plaintext from bob's address. east and west refuse all four. The real IKE_SA does not change. | `AUTH_FAILED`, `AUTH_FAILED`, `NO_PROPOSAL_CHOSEN`, `XfrmInTmplMismatch` 3 |
| `lifecycle` | west loads a renewed certificate and key and reauthenticates under load. Then west loads an expired certificate, and after that east loads a stale CRL. east refuses each reauthentication, but the old IKE_SA keeps traffic flowing until the tunnel is terminated. | 59 or 60 of 60 pings across the renewal |

## Findings

These came out of building the lab. Each is a comment in the code.

1. **A tunnel can stay down after the far end restarts.** With `start_action = start`, west's first IKE_SA_INIT reached east 80 ms before east had loaded its configuration. east answered `NO_PROPOSAL_CHOSEN`, and charon does not retry after that answer, whatever `keyingtries` says. `start_action = trap|start` fixes it: the trap policy stays, and the next packet for the tunnel starts a new attempt.
2. **Linux encrypts first and fragments after.** A packet without DF that does not fit becomes one ESP packet in two outer IP fragments. The far gateway must reassemble them before it can decrypt. AWS recommends fragmenting before encryption.
3. **MSS clamping to the route MTU does not fix a narrower hop farther away.** The gateway only knows its own interface. If the path drops large packets without ICMP, clamp to a fixed value below the real path MTU.
4. **UDP gets through by fragmentation or not at all.** An application that sets DF loses the first datagram silently. It must handle `EMSGSIZE` and send smaller.
5. **`unique = never` is needed when several tunnels share one pair of identities.** With the default, INITIAL_CONTACT on one tunnel removes the IKE_SA of the other.
6. **An expired certificate does not take the tunnel down when it expires.** A refused reauthentication leaves the old IKE_SA in place, and `swanctl --rekey --reauth` still reports success. The outage comes later, at the next reboot, DPD timeout or terminate. Monitor the expiry date, not the tunnel state.
7. **A stale CRL is as bad as an expired certificate.** With `revocation = strict`, a CRL past its nextUpdate refuses every peer: `RULE_CRL_VALIDATION is STALE, but requires at least GOOD`. `pki --signcrl` gives a CRL 15 days by default, so somebody must re-sign it on time.
8. **Renewal is make-before-break.** Load the new certificate and key with `swanctl --load-creds`, then reauthenticate. In 22 tries, 3 reauthentications lost one ping each, and a rekey never lost one.

## Run it

From the root of the vivarium repository (`vivarium/` in nixidae):

```sh
nix build --file . ipsec                     # the whole run, in the sandbox
nix run --file . ipsec.driver -- --out ./out --break failover
```

`--break` pauses before the phase you name, with all guests up. The run
prints the full path of the `vivarium` command; use it in place of
`vivarium` below. From another shell you can then look around:

```sh
vivarium ctl --out ./out exec 'print(await west.succeed("swanctl --list-sas"))'
vivarium ctl --out ./out exec 'print(await west.succeed("ip xfrm state"))'
vivarium ctl --out ./out continue
```

## What this is not

- **One implementation.** Both ends run strongSwan on Linux. There is no Palo Alto, Cisco or AWS endpoint here, only the same layout.
- **No NAT.** Both gateways have public addresses, so NAT-T (UDP 4500) is not used.
- **Test keys.** `pki.nix` makes the PKI at build time, so the private keys are in the Nix store. That is correct for a lab and wrong for anything else.

# Two strongSwan gateways, west and east, on a shared "internet".
#
#   policy-based   alice 10.1.0.0/24  <-west==east->  bob  10.2.0.0/24
#                  one CHILD_SA whose traffic selectors are the two LANs.
#   route-based    carol 10.11.0.0/24 <-west==east->  dave 10.12.0.0/24
#                  two tunnels with 0.0.0.0/0 selectors into XFRM
#                  interfaces, eBGP over each, laid out like an AWS
#                  Site-to-Site VPN: east is the AWS end (ASN 64512, two
#                  outside addresses, .1 inside each /30), west the
#                  customer gateway (.2).
#   netmap         erin 10.50.0.10 <-west==east-> frank 10.50.0.10, the
#                  same subnet on both sides, joined by 1:1 prefix NAT
#                  over a second policy CHILD_SA and over the BGP tunnels.
#
# Both gateways authenticate with ECDSA certificates and check the peer's
# against a CRL with `revocation = strict`. east takes any outside address
# (`remote_addrs = %any`), as AWS does for a certificate-based customer
# gateway, so the certificate is the only thing that admits a peer.
# mallory is on the internet: it claims west's identity three ways, and
# sends plaintext from bob's address. None of it gets anywhere.
{
  lib,
  pkgs,
  pki,
}:
let
  bare = cidr: lib.head (lib.splitString "/" cidr);
  otherSide = side: if side == "west" then "east" else "west";

  topology = {
    west = {
      internet = [ "192.0.2.1/24" ];
      lan = [
        "10.1.0.1/24"
        "10.11.0.1/24"
      ];
      id = "west.vpn.test";
      asn = 65000;
    };
    east = {
      internet = [
        "192.0.2.2/24"
        "192.0.2.21/24"
        "192.0.2.22/24"
      ];
      lan = [
        "10.2.0.1/24"
        "10.12.0.1/24"
      ];
      id = "east.vpn.test";
      asn = 64512;
    };
    mallory.internet = [ "192.0.2.66/24" ];

    policy = {
      west = "10.1.0.0/24";
      east = "10.2.0.0/24";
      eastAddress = "192.0.2.2";
    };

    # AWS numbers its two tunnels' inside /30s from 169.254.0.0/16 and
    # takes the first host address itself.
    tunnels = {
      "1" = {
        eastAddress = "192.0.2.21";
        inside = "169.254.10.0/30";
        east = "169.254.10.1";
        west = "169.254.10.2";
      };
      "2" = {
        eastAddress = "192.0.2.22";
        inside = "169.254.11.0/30";
        east = "169.254.11.1";
        west = "169.254.11.2";
      };
    };
    routed = {
      west = "10.11.0.0/24";
      east = "10.12.0.0/24";
      # east announces this as well; west's inbound prefix list drops it.
      bogus = "10.99.0.0/24";
    };
    tunnelMtu = 1400;

    # The same /24 behind both gateways, on purpose. Each gateway maps it
    # 1:1 to a range of its own per path, so the far side only ever sees
    # the mapped addresses.
    netmap = {
      inside = "10.50.0.0/24";
      gateway = "10.50.0.1/24";
      west = {
        policy = "100.64.1.0/24";
        routed = "100.64.11.0/24";
      };
      east = {
        policy = "100.64.2.0/24";
        routed = "100.64.12.0/24";
      };
    };

    hosts = {
      erin = {
        gateway = "west";
        segment = "nat";
        address = "10.50.0.10/24";
        via = "10.50.0.1";
      };
      frank = {
        gateway = "east";
        segment = "nat";
        address = "10.50.0.10/24";
        via = "10.50.0.1";
      };
      alice = {
        gateway = "west";
        address = "10.1.0.10/24";
        via = "10.1.0.1";
      };
      bob = {
        gateway = "east";
        address = "10.2.0.10/24";
        via = "10.2.0.1";
      };
      carol = {
        gateway = "west";
        address = "10.11.0.10/24";
        via = "10.11.0.1";
      };
      dave = {
        gateway = "east";
        address = "10.12.0.10/24";
        via = "10.12.0.1";
      };
    };
  };

  ikeProposals = [ "aes256gcm16-prfsha384-ecp384" ];
  espProposals = [ "aes256gcm16-ecp384" ];

  # Fast enough for a test to watch a peer die: a request waits 1s,
  # 1.2s, 1.44s and 1.728s (timeout * base^n), so the peer is declared
  # dead about 5.4s after the first try. The defaults (4s, base 1.8, 5
  # tries) take 165s.
  #
  # make_before_break: a reauthentication builds the new IKE_SA before it
  # deletes the old one. With `no` a refused reauthentication takes the
  # tunnel down at once, and the lifecycle phase measures the other case.
  strongswanConf = ''
    charon {
      retransmit_timeout = 1.0
      retransmit_base = 1.2
      retransmit_tries = 3
      make_before_break = yes
    }
    charon-systemd {
      journal {
        default = 1
        ike = 2
        cfg = 2
      }
    }
  '';

  credentials = name: files: {
    environment.etc = lib.listToAttrs (
      map (
        { dir, file }:
        lib.nameValuePair "swanctl/${dir}/${file}" {
          source = "${pki}/${file}";
          mode = if dir == "private" then "0400" else "0444";
        }
      ) files
    );
  };

  peerAuth = id: {
    auth = "pubkey";
    inherit id;
    cacerts = [ "ca.pem" ];
    revocation = "strict";
  };

  connection =
    {
      local,
      remote,
      localAddress,
      remoteAddress,
      children,
    }:
    {
      version = 2;
      local_addrs = [ localAddress ];
      remote_addrs = [ remoteAddress ];
      proposals = ikeProposals;
      # Every connection carries the same pair of identities. The default
      # `no` honours INITIAL_CONTACT, and that would tear down a sibling
      # tunnel's IKE_SA as a stale duplicate.
      unique = "never";
      keyingtries = 0;
      # strongSwan's default, set so the README does not depend on it.
      rekey_time = "4h";
      dpd_delay = "2s";
      mobike = false;
      send_cert = "always";
      local.main = {
        auth = "pubkey";
        id = local;
        certs = [ "${lib.removeSuffix ".vpn.test" local}.pem" ];
      };
      remote.main = peerAuth remote;
      inherit children;
    };

  # west initiates and restarts; east answers and clears, as an AWS
  # endpoint does.
  #
  # `trap|start`, not `start`: a peer that has not loaded its connections
  # yet answers NO_PROPOSAL_CHOSEN, which charon takes as final whatever
  # keyingtries says. Measured at boot here: west's IKE_SA_INIT reached
  # east 80 ms before east's swanctl loaded. The trap policy stays and
  # the next packet for the tunnel (BGP retries every second) initiates
  # again.
  childFor =
    side: extra:
    {
      esp_proposals = espProposals;
      mode = "tunnel";
      rekey_time = "1h";
    }
    // (
      if side == "west" then
        {
          start_action = "trap|start";
          dpd_action = "restart";
          close_action = "trap";
        }
      else
        {
          start_action = "none";
          dpd_action = "clear";
          close_action = "none";
        }
    )
    // extra;

  connections =
    side:
    let
      other = if side == "west" then "east" else "west";
      me = topology.${side};
      address = cidr: bare cidr;
      westAddress = address (lib.head topology.west.internet);
      remoteFor = eastAddress: if side == "west" then eastAddress else "%any";
      localFor = eastAddress: if side == "west" then westAddress else eastAddress;
    in
    {
      policy = connection {
        local = me.id;
        remote = topology.${other}.id;
        localAddress = localFor topology.policy.eastAddress;
        remoteAddress = remoteFor topology.policy.eastAddress;
        children.policy = childFor side {
          local_ts = [ topology.policy.${side} ];
          remote_ts = [ topology.policy.${other} ];
        };
        # A second CHILD_SA under the same IKE_SA, between the mapped
        # ranges. The real 10.50.0.0/24 is in no selector.
        children.netmap = childFor side {
          local_ts = [ topology.netmap.${side}.policy ];
          remote_ts = [ topology.netmap.${other}.policy ];
        };
      };
    }
    // lib.mapAttrs' (
      n: tunnel:
      lib.nameValuePair "tunnel${n}" (connection {
        local = me.id;
        remote = topology.${other}.id;
        localAddress = localFor tunnel.eastAddress;
        remoteAddress = remoteFor tunnel.eastAddress;
        children."tunnel${n}" = childFor side {
          local_ts = [ "0.0.0.0/0" ];
          remote_ts = [ "0.0.0.0/0" ];
          if_id_in = n;
          if_id_out = n;
        };
      })
    ) topology.tunnels;

  xfrmInterfaces =
    side:
    lib.foldl' lib.recursiveUpdate { } (
      lib.mapAttrsToList (n: tunnel: {
        systemd.network.netdevs."30-ipsec${n}" = {
          netdevConfig = {
            Kind = "xfrm";
            Name = "ipsec${n}";
            MTUBytes = toString topology.tunnelMtu;
          };
          xfrmConfig = {
            InterfaceId = lib.toInt n;
            Independent = true;
          };
        };
        systemd.network.networks."30-ipsec${n}" = {
          matchConfig.Name = "ipsec${n}";
          address = [ "${tunnel.${side}}/30" ];
          networkConfig = {
            LinkLocalAddressing = "no";
            ConfigureWithoutCarrier = true;
          };
          linkConfig.RequiredForOnline = "no";
        };
      }) topology.tunnels
    );

  bgp =
    side:
    let
      other = if side == "west" then "east" else "west";
      me = topology.${side};
      neighbors = lib.mapAttrsToList (_: tunnel: tunnel.${other}) topology.tunnels;
      mapped = topology.netmap.${side}.routed;
      announced = [
        topology.routed.${side}
        mapped
      ]
      ++ lib.optional (side == "east") topology.routed.bogus;
    in
    {
      services.frr = {
        bgpd.enable = true;
        # `network` announces only what the RIB holds. The mapped range is
        # no real subnet, so a blackhole route stands in for it; DNAT in
        # prerouting rewrites traffic for it before the route is looked up.
        config = ''
          frr defaults traditional
          log syslog informational
          ${lib.optionalString (side == "east") "ip route ${topology.routed.bogus} blackhole"}
          ip route ${mapped} blackhole
          ip prefix-list ${lib.toUpper other}-IN seq 10 permit ${topology.routed.${other}}
          ip prefix-list ${lib.toUpper other}-IN seq 15 permit ${topology.netmap.${other}.routed}
          ip prefix-list ${lib.toUpper side}-OUT seq 10 permit ${topology.routed.${side}}
          ip prefix-list ${lib.toUpper side}-OUT seq 15 permit ${mapped}
          ${lib.optionalString (
            side == "east"
          ) "ip prefix-list ${lib.toUpper side}-OUT seq 20 permit ${topology.routed.bogus}"}
          route-map ${lib.toUpper other}-IN permit 10
           match ip address prefix-list ${lib.toUpper other}-IN
          exit
          route-map ${lib.toUpper side}-OUT permit 10
           match ip address prefix-list ${lib.toUpper side}-OUT
          exit
          router bgp ${toString me.asn}
           bgp router-id ${bare (lib.head me.internet)}
           no bgp default ipv4-unicast
          ${lib.concatMapStrings (peer: ''
            neighbor ${peer} remote-as ${toString topology.${other}.asn}
            neighbor ${peer} timers 1 3
            neighbor ${peer} timers connect 1
          '') neighbors}
           address-family ipv4 unicast
          ${lib.concatMapStrings (prefix: "  network ${prefix}\n") announced}
            maximum-paths 2
          ${lib.concatMapStrings (peer: ''
            neighbor ${peer} activate
            neighbor ${peer} soft-reconfiguration inbound
            neighbor ${peer} route-map ${lib.toUpper other}-IN in
            neighbor ${peer} route-map ${lib.toUpper side}-OUT out
          '') neighbors}
           exit-address-family
          exit
        '';
      };
    };

  # Default drop both ways. A LAN reaches the far LAN only through its
  # tunnel: policy traffic must arrive with an IPsec secpath, routed
  # traffic only on an XFRM interface. Nothing is forwarded to vec0, the
  # uplink to the host, so a broken tunnel cannot leak out of it.
  firewall =
    side:
    let
      other = if side == "west" then "east" else "west";
      inherit (topology.netmap) inside;
      near = topology.netmap.${side};
      far = topology.netmap.${other};
    in
    {
      networking.nftables = {
        enable = true;
        ruleset = ''
          table inet lab {
            chain input {
              type filter hook input priority filter; policy drop;
              iifname { "lo", "vec0" } accept
              ct state established,related accept
              ct state invalid drop
              meta l4proto { icmp, ipv6-icmp } accept
              iifname "internet" udp dport { 500, 4500 } accept
              iifname "internet" meta l4proto esp accept
              iifname "ipsec*" tcp dport 179 accept
              counter log prefix "lab input drop: " level info drop
            }
            chain forward {
              type filter hook forward priority filter; policy drop;
              ct state established,related accept
              ct state invalid drop
              iifname "lan" ip saddr ${topology.policy.${side}} ip daddr ${topology.policy.${other}} accept
              iifname "internet" ip saddr ${topology.policy.${other}} ip daddr ${topology.policy.${side}} \
                meta secpath exists accept
              iifname "lan" oifname "ipsec*" ip saddr ${topology.routed.${side}} ip daddr ${topology.routed.${other}} accept
              iifname "ipsec*" oifname "lan" ip saddr ${topology.routed.${other}} ip daddr ${topology.routed.${side}} accept
              # The forward hook sees the inside address: after DNAT, before SNAT.
              iifname "nat" oifname "internet" ip saddr ${inside} ip daddr ${far.policy} accept
              iifname "nat" oifname "ipsec*" ip saddr ${inside} ip daddr ${far.routed} accept
              iifname "internet" oifname "nat" ip saddr ${far.policy} ip daddr ${inside} \
                meta secpath exists accept
              iifname "ipsec*" oifname "nat" ip saddr ${far.routed} ip daddr ${inside} accept
              counter log prefix "lab forward drop: " level info drop
            }
          }

          # 1:1 prefix NAT: 10.50.0.N is near.policy .N towards the far
          # policy range and near.routed .N towards the far routed range.
          table ip netmap {
            chain prerouting {
              type nat hook prerouting priority dstnat;
              ip daddr ${near.policy} dnat ip prefix to ${inside}
              ip daddr ${near.routed} dnat ip prefix to ${inside}
            }
            chain postrouting {
              type nat hook postrouting priority srcnat;
              ip saddr ${inside} ip daddr ${far.policy} snat ip prefix to ${near.policy}
              ip saddr ${inside} ip daddr ${far.routed} snat ip prefix to ${near.routed}
            }
          }
        '';
      };
    };

  gateway = side: {
    vivarium.interfaces = {
      internet = {
        segment = "internet";
        addresses = topology.${side}.internet;
      };
      lan = {
        segment = "${side}-lan";
        addresses = topology.${side}.lan;
      };
      nat = {
        segment = "${side}-nat";
        addresses = [ topology.netmap.gateway ];
      };
    };
    # charon routes a CHILD_SA's remote selector in table 220 only when
    # the gateway holds an address in the local one. Measured: no route
    # for the far policy-mapped range, so it went to the default route,
    # vec0, and the forward chain dropped it.
    networking.interfaces.internet.ipv4.routes = [
      {
        address = bare topology.netmap.${otherSide side}.policy;
        prefixLength = 24;
        via = if side == "west" then topology.policy.eastAddress else bare (lib.head topology.west.internet);
      }
    ];
    boot.kernel.sysctl = {
      "net.ipv4.ip_forward" = 1;
      # Loose: with two tunnels a reply can come back on the other one.
      "net.ipv4.conf.all.rp_filter" = 2;
      "net.ipv4.conf.default.rp_filter" = 2;
    };
    environment.systemPackages = [
      pkgs.strongswan
      pkgs.conntrack-tools
    ];
    services.strongswan-swanctl = {
      enable = true;
      strongswan.extraConfig = strongswanConf;
      swanctl.connections = connections side;
    };
    imports = [
      (credentials side [
        {
          dir = "x509ca";
          file = "ca.pem";
        }
        {
          dir = "x509crl";
          file = "ca.crl";
        }
        {
          dir = "x509";
          file = "${side}.pem";
        }
        {
          dir = "private";
          file = "${side}.key";
        }
      ])
      (spares side)
      (xfrmInterfaces side)
      (bgp side)
      (firewall side)
    ];
  };

  # Outside /etc/swanctl, so charon does not load them: the lifecycle
  # phase swaps them in.
  spares =
    side:
    let
      files = {
        west = [
          "renewed.pem"
          "renewed.key"
          "expired.pem"
          "expired.key"
        ];
        east = [ "stale.crl" ];
      };
    in
    {
      environment.etc = lib.listToAttrs (
        map (
          file:
          lib.nameValuePair "ipsec-lab/${file}" {
            source = "${pki}/${file}";
            mode = "0400";
          }
        ) files.${side}
      );
    };

  # Three ways to be west without west's key, each a connection of its
  # own and none started at boot.
  mallory =
    let
      impostor =
        {
          cert,
          proposals ? ikeProposals,
        }:
        {
          version = 2;
          local_addrs = [ (bare (lib.head topology.mallory.internet)) ];
          remote_addrs = [ topology.policy.eastAddress ];
          inherit proposals;
          keyingtries = 1;
          send_cert = "always";
          local.main = {
            auth = "pubkey";
            id = topology.west.id;
            certs = [ cert ];
          };
          remote.main = {
            auth = "pubkey";
            id = topology.east.id;
          };
          children.steal = {
            local_ts = [ topology.policy.west ];
            remote_ts = [ topology.policy.east ];
            esp_proposals = espProposals;
            start_action = "none";
          };
        };
    in
    {
      vivarium.interfaces.internet = {
        segment = "internet";
        addresses = topology.mallory.internet;
      };
      environment.systemPackages = [ pkgs.strongswan ];
      services.strongswan-swanctl = {
        enable = true;
        strongswan.extraConfig = strongswanConf;
        swanctl.connections = {
          # west's real identity and a certificate the CA has revoked.
          revoked = impostor { cert = "revoked.pem"; };
          # west's identity signed by a CA east does not trust.
          rogue = impostor { cert = "rogue.pem"; };
          # A valid-looking chain, but a proposal east does not offer.
          weak = impostor {
            cert = "revoked.pem";
            proposals = [ "aes128-sha1-modp1024" ];
          };
        };
      };
      imports = [
        (credentials "mallory" [
          {
            dir = "x509";
            file = "revoked.pem";
          }
          {
            dir = "private";
            file = "revoked.key";
          }
          {
            dir = "x509";
            file = "rogue.pem";
          }
          {
            dir = "private";
            file = "rogue.key";
          }
        ])
      ];
    };

  host = name: host: {
    vivarium.interfaces.lan = {
      segment = "${host.gateway}-${host.segment or "lan"}";
      addresses = [ host.address ];
    };
    # Everything lab-side through the gateway; vec0 keeps the default.
    networking.interfaces.lan.ipv4.routes = [
      {
        address = "10.0.0.0";
        prefixLength = 8;
        via = host.via;
      }
      {
        address = "100.64.0.0";
        prefixLength = 10;
        via = host.via;
      }
    ];
  };

  nodes = {
    west = gateway "west";
    east = gateway "east";
    inherit mallory;
  }
  // lib.mapAttrs host topology.hosts;

  # What the phases need, without reading Nix.
  settings = {
    inherit (topology)
      policy
      tunnels
      routed
      tunnelMtu
      netmap
      ;
    ids = {
      west = topology.west.id;
      east = topology.east.id;
    };
    asn = {
      west = topology.west.asn;
      east = topology.east.asn;
    };
    westAddress = bare (lib.head topology.west.internet);
    hosts = lib.mapAttrs (_: host: {
      inherit (host) gateway;
      address = bare host.address;
    }) topology.hosts;
  };
in
{
  inherit nodes settings;
}

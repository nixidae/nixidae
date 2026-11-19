/*
  The router guest (nixidae/nixidae#1404, docs/design/router-lab.md).

  A run with `router.enable` has one: every other guest's uplink (`vec0`)
  is a segment of its own to this guest, and the router bridges them into
  the uplink's subnet (nix/uplink.nix). Each guest leases its own address by its uplink MAC. TCP,
  UDP and ICMP echo to anywhere off the bridge go to `relay`, which
  carries each flow over vsock to the egress on the host.

  A guest with `vivarium.uplink.proxy = "vsock"` is its own router: the
  same ruleset and relay in a network namespace, behind a veth pair whose
  guest end is `vec0`. AF_VSOCK works from that namespace: the kernel's
  `net.vsock.ns_mode` is `global`. A container has no vsock device: its
  relay uses two unix sockets in the directory the runner binds at
  /run/host/agent (nixidae/nixidae#1522).
*/
{
  config,
  lib,
  pkgs,
  hostPkgs,
  nodes,
  utils,
  ...
}:
let
  cfg = config.vivarium.router;
  inherit (lib) mkOption types;

  bridge = "uplink";
  uplink = import ../nix/uplink.nix;
  inherit (uplink)
    subnet
    gateway
    dns
    prefix6
    gateway6
    ;

  relay = pkgs.callPackage ../pkgs/vivarium-egress/package.nix { };
  # vhost-user is Linux's (epoll, eventfd). A macOS host runs krun only,
  # whose vsock is libkrun's unix sockets.
  egress = hostPkgs.callPackage ../pkgs/vivarium-egress/package.nix {
    vhostUser = hostPkgs.stdenv.hostPlatform.isLinux;
  };

  # The guests behind this router, by name. Not this guest itself.
  guests = lib.filterAttrs (
    name: node: name != config.networking.hostName && node.vivarium.uplink.proxy == "router"
  ) nodes;

  # The router's port to a guest: at most 15 characters, so its index.
  port = node: "g${toString node.vivarium.index}";

  # The uplink's routing table on the router: the bridge is a port of
  # this VRF, and the relay binds every socket to it (nixidae/nixidae#1523).
  vrf = "vrf-${bridge}";

  /*
    Everything a network's guests send off its subnets goes to its relay.
    TCP by `redirect`, so the relay reads SO_ORIGINAL_DST; UDP by tproxy,
    and ICMP echo by mark 1 to a raw socket. The network's own traffic --
    DHCP, broadcast, multicast, neighbour discovery, a guest to a guest --
    stays.

    `tproxyIn` is the device `tproxy` matches: the network's VRF. A VRF
    runs prerouting a second time with itself as the input device, and a
    socket that `tproxy` assigns on the first pass is lost (E11,
    router-lab.md). nft masks a CIDR's host bits, so a router address
    with its prefix length names its subnet.

    `extra4` and `extra6` take further rules into the same chains, a list
    by chain: the uplink's gateway and DNS addresses.
  */
  offNet =
    {
      table,
      device,
      tproxyIn,
      subnets4,
      subnets6,
      tcp,
      udp,
      extra4 ? { },
      extra6 ? { },
    }:
    let
      family =
        name: daddr: echo: onlink: extra:
        let
          away = "${daddr} != { ${lib.concatStringsSep ", " onlink} }";
          rules = chain: lib.concatMapStrings (rule: "\n    ${rule}") (extra.${chain} or [ ]);
        in
        ''
          table ${name} ${table} {
            chain prerouting {
              type nat hook prerouting priority dstnat;
              iifname "${device}" ${away} meta l4proto tcp redirect to :${toString tcp}${rules "prerouting"}
            }
            chain mangle {
              type filter hook prerouting priority mangle;
              iifname "${tproxyIn}" ${away} meta l4proto udp meta mark set 1 tproxy to :${toString udp}
              iifname "${device}" ${away} ${echo} meta mark set 1${rules "mangle"}
            }
          }
        '';
    in
    family "ip" "ip daddr" "icmp type echo-request" (
      subnets4
      ++ [
        "224.0.0.0/4"
        "255.255.255.255"
      ]
    ) extra4
    + family "ip6" "ip6 daddr" "icmpv6 type echo-request" (
      subnets6
      ++ [
        "fe80::/10"
        "ff00::/8"
      ]
    ) extra6;

  # The uplink's rules, and its gateway and DNS addresses to the relay
  # too. IPv6 has no DNS address of its own.
  ruleset =
    tproxyIn:
    offNet {
      table = "router";
      device = bridge;
      inherit tproxyIn;
      subnets4 = [ subnet ];
      subnets6 = [ "${prefix6}/64" ];
      tcp = 9000;
      udp = 9001;
      extra4 = {
        prerouting = [
          "iifname \"${bridge}\" ip daddr ${gateway} meta l4proto tcp redirect to :9000"
          "iifname \"${bridge}\" ip daddr ${dns} tcp dport 53 redirect to :9000"
        ];
        mangle = [
          "iifname \"${tproxyIn}\" ip daddr ${gateway} udp dport != 67 meta mark set 1 tproxy to :9001"
          "iifname \"${tproxyIn}\" ip daddr ${dns} udp dport 53 meta mark set 1 tproxy to :9001"
          "iifname \"${bridge}\" ip daddr ${gateway} icmp type echo-request meta mark set 1"
        ];
      };
      extra6 = {
        prerouting = [
          "iifname \"${bridge}\" ip6 daddr ${gateway6} meta l4proto tcp redirect to :9000"
        ];
        mangle = [
          "iifname \"${tproxyIn}\" ip6 daddr ${gateway6} meta l4proto udp meta mark set 1 tproxy to :9001"
          "iifname \"${bridge}\" ip6 daddr ${gateway6} icmpv6 type echo-request meta mark set 1"
        ];
      };
    };

  /*
    The router's networks besides the uplink: each segment with a
    `router`, in order of name. Network n has VRF table 1000 + n, relay
    ports 9000 + 2n and 9001 + 2n, and the egress's vsock port 5000 + 2n,
    after the uplink's 1000, 9000, 9001 and 5000. 5001 + 2n stays free for
    the network's forwards, as 5001 is the uplink's. The router's
    interface on the segment has the segment's name.
  */
  networks = lib.imap1 (
    n: name:
    let
      inherit (config.vivarium.segments.${name}) router;
    in
    {
      inherit name router;
      vrf = "vrf-${name}";
      table = 1000 + n;
      tcp = 9000 + 2 * n;
      udp = 9001 + 2 * n;
      egress = 5000 + 2 * n;
    }
  ) (lib.attrNames (lib.filterAttrs (_: segment: segment.router != null) config.vivarium.segments));

  deviceUnit = name: "sys-subsystem-net-devices-${utils.escapeSystemdPath name}.device";

  # The uplink's egress end and forwards' end. The runner's container
  # backend names the same two sockets. A router network has its own.
  ends =
    if config.vivarium.backend == "container" then
      {
        egress = "unix:/run/host/agent/egress.sock";
        inward = "unix:/run/host/agent/relay.sock";
      }
    else
      {
        egress = "5000";
        inward = "5001";
      };

  relayService =
    {
      tcp,
      udp,
      egress ? ends.egress,
      inward,
    }:
    {
      wantedBy = [ "multi-user.target" ];
      environment = {
        # E8: a 4 MiB vsock window; the yield after a short read.
        RELAY_VSOCK_BUFFER = "4194304";
        RELAY_YIELD = "1";
      };
      serviceConfig = {
        ExecStart = "${relay}/bin/relay ${toString tcp} ${egress} ${toString udp} ${inward}";
        Restart = "always";
      };
    };

  # E11: a relay socket bound to no device routes in the main table, and a
  # reply into the VRF fails. A bind before networkd makes the VRF fails
  # too.
  inVrf = name: {
    environment.RELAY_DEVICE = name;
    bindsTo = [ (deviceUnit name) ];
    after = [
      "nftables.service"
      (deviceUnit name)
    ];
  };

  # A `vsock` guest's own router: the relay and the ruleset in this
  # namespace, the far end of `vec0` named as the router's bridge.
  netns = "uplink";
in
{
  options.vivarium.router = {
    enable = mkOption {
      type = types.bool;
      default = false;
      internal = true;
      description = ''
        This guest is the run's router. `mkTest` sets it on the guest it
        adds for `router.enable`.
      '';
    };
    address = mkOption {
      type = types.functionTo types.str;
      internal = true;
      readOnly = true;
      default = uplink.address;
      description = "A routed guest's uplink address, by its `vivarium.index`.";
    };
    address6 = mkOption {
      type = types.functionTo types.str;
      internal = true;
      readOnly = true;
      default = uplink.address6;
      description = ''
        A routed guest's IPv6 uplink address, by its `vivarium.index`: the
        router's prefix and the digits of its IPv4 address's last octet.
      '';
    };
    networkPorts = mkOption {
      type = types.listOf types.port;
      internal = true;
      readOnly = true;
      default = map (net: net.egress) networks;
      description = ''
        The vsock port on which each router network's relay reaches the
        egress. The egress knows a flow's network by the port. The runner
        gives each one to the egress on krun.
      '';
    };
  };

  config = lib.mkMerge [
    # A guest behind the router: its address from the router's
    # advertisement, the host part fixed by its index.
    (lib.mkIf (config.vivarium.uplink.proxy == "router") {
      systemd.network.networks."40-vec0".ipv6AcceptRAConfig.Token =
        "static:::${toString (uplink.host config.vivarium.index)}";
      # The router's port, lease and forwards for a guest are fixed at
      # evaluation; an instance arrives later and would get none.
      assertions = [
        {
          assertion = !config.vivarium.template;
          message = "a template's instances cannot sit behind the router (nixidae/nixidae#1404); set its vivarium.uplink.proxy.";
        }
      ];
    })
    (lib.mkIf cfg.enable {
      # 256M on krun runs out of memory at boot, and on QEMU under load,
      # after seconds-long stalls into the guest. UML runs in it.
      vivarium.memory = lib.mkDefault (if config.vivarium.backend == "uml" then "256M" else "512M");
      # E2: two carry 16 Gbit/s on krun, and one is the limit. Every host
      # core, krun's default, fills 256M with per-CPU state before boot ends.
      vivarium.cpus = lib.mkDefault 2;
      # A TX completion interrupt costs krun about 14 us: 237k of them filled
      # a vCPU. Off, host to guest went from 8.6-8.7 to 12.2-13.7 Gbit/s,
      # guest to host stayed at 16.6-18.5 (2026-11-17, router-lab.md).
      boot.kernelParams = lib.mkIf (config.vivarium.backend == "krun") [ "virtio_net.napi_tx=0" ];
      # Not behind itself: the runner gives the router vsock to the host
      # and no vec0 (`router` in its spec). Forced over a run's `defaults`
      # that name the other guests' uplink.
      vivarium.uplink.proxy = lib.mkForce "passt";
      # Its egress binds nothing for it: no ssh, and nothing to reach.
      vivarium.inbound = lib.mkForce [ ];
      vivarium.launch.config.egress = "${egress}/bin/egress";

      vivarium.interfaces = lib.mapAttrs' (
        _: node: lib.nameValuePair (port node) { segment = node.vivarium.uplink.segment; }
      ) guests;

      assertions = [
        {
          # Each reaches the egress over vsock: UML and QEMU through the
          # egress's own vhost-user device, krun through libkrun's.
          assertion = lib.elem config.vivarium.backend [
            "uml"
            "qemu"
            "krun"
          ];
          message = "the router guest runs on UML, QEMU or krun; it is ${config.vivarium.backend}.";
        }
        {
          # `uplink.address index` stays below the pool at .215.
          assertion = lib.all (node: node.vivarium.index < 200) (lib.attrValues guests);
          message = "the router gives addresses to 200 guests at most.";
        }
      ];

      # What a router does not use (E10: 6.4 s and 129 MiB on UML against
      # 7.6 s and 155 MiB). udev stays: without it networkd configures no link.
      vivarium.nixDatabase.enable = false;
      nix.enable = false;
      systemd.oomd.enable = false;
      services.timesyncd.enable = false;
      security.audit.enable = false;
      security.enableWrappers = false;
      environment.defaultPackages = [ ];
      services.openssh.enable = lib.mkForce false;
      services.resolved.enable = false;
      services.nscd.enable = false;
      system.nssModules = lib.mkForce [ ];
      systemd.suppressedSystemUnits = [
        "systemd-journal-catalog-update.service"
        "systemd-update-done.service"
        "lastlog2-import.service"
      ];

      boot.kernel.sysctl = {
        # The relay answers echo requests with the host's replies.
        "net.ipv4.icmp_echo_ignore_all" = 1;
        "net.ipv6.icmp.echo_ignore_all" = 1;
        # br_netfilter is built in: without these, a frame from one guest to
        # another would cross the ip hooks above and reach the relay.
        "net.bridge.bridge-nf-call-iptables" = 0;
        "net.bridge.bridge-nf-call-ip6tables" = 0;
        "net.bridge.bridge-nf-call-arptables" = 0;
      };

      systemd.network.netdevs."10-${vrf}" = {
        netdevConfig = {
          Kind = "vrf";
          Name = vrf;
        };
        vrfConfig.Table = 1000;
      };
      systemd.network.netdevs."20-${bridge}".netdevConfig = {
        Kind = "bridge";
        Name = bridge;
        MTUBytes = toString config.vivarium.mtu;
      };
      systemd.network.networks = {
        "30-${vrf}" = {
          matchConfig.Name = vrf;
          networkConfig.ConfigureWithoutCarrier = true;
        };
        "40-${bridge}" = {
          matchConfig.Name = bridge;
          vrf = [ vrf ];
          address = [
            "${gateway}/24"
            "${dns}/24"
            "${gateway6}/64"
          ];
          networkConfig = {
            DHCPServer = true;
            ConfigureWithoutCarrier = true;
            IPv6SendRA = true;
            IPv6AcceptRA = false;
            # The gateway address is the router's own; no detection delay.
            IPv6DuplicateAddressDetection = 0;
          };
          # The guests' resolver is `dns`; IPv6 carries no DNS.
          ipv6SendRAConfig = {
            EmitDNS = false;
            EmitDomains = false;
          };
          ipv6Prefixes = [ { Prefix = "${prefix6}/64"; } ];
          dhcpServerConfig = {
            ServerAddress = "${gateway}/24";
            EmitRouter = true;
            Router = gateway;
            EmitDNS = true;
            DNS = [ dns ];
            # Every guest has a static lease; the pool is for anything else.
            PoolOffset = 200;
            PoolSize = 50;
          };
          # Mark 1 is local: tproxy and the raw socket take it there. Here and
          # not by hand: networkd removes a rule it does not manage, and UDP
          # and ICMP off the bridge then never reach the relay.
          routingPolicyRules = [
            {
              FirewallMark = 1;
              Table = 100;
              Family = "both";
            }
          ];
          routes = [
            {
              Destination = "0.0.0.0/0";
              Type = "local";
              Table = 100;
            }
            {
              Destination = "::/0";
              Type = "local";
              Table = 100;
            }
          ];
          dhcpServerStaticLeases = lib.mapAttrsToList (_: node: {
            MACAddress = node.vivarium.uplinkMac;
            Address = cfg.address node.vivarium.index;
          }) guests;
        };
      }
      // lib.mapAttrs' (
        _: node: lib.nameValuePair "40-${port node}" { networkConfig.Bridge = bridge; }
      ) guests;

      networking.nftables = {
        enable = true;
        # tproxy needs the kernel's module; the build sandbox's nft lacks it.
        checkRuleset = false;
        ruleset = ruleset vrf;
      };

      systemd.services.vivarium-relay = lib.mkMerge [
        (relayService {
          tcp = 9000;
          udp = 9001;
          inherit (ends) inward;
        })
        (inVrf vrf)
        { description = "The router's relay to the host"; }
      ];
    })
    (lib.mkIf cfg.enable {
      assertions = map (net: {
        assertion =
          lib.stringLength net.name <= 11 && net.name != bridge && builtins.match "g[0-9]+" net.name == null;
        message = "segments.${net.name}.router: the router names its interface after the segment and its VRF vrf-${net.name}, so the name is at most 11 characters, not ${bridge}, and not g and a number.";
      }) networks;

      vivarium.interfaces = lib.listToAttrs (
        map (net: lib.nameValuePair net.name { segment = net.name; }) networks
      );

      systemd.network.netdevs = lib.listToAttrs (
        map (
          net:
          lib.nameValuePair "10-${net.vrf}" {
            netdevConfig = {
              Kind = "vrf";
              Name = net.vrf;
            };
            vrfConfig.Table = net.table;
          }
        ) networks
      );

      systemd.network.networks = lib.listToAttrs (
        lib.concatMap (net: [
          (lib.nameValuePair "30-${net.vrf}" {
            matchConfig.Name = net.vrf;
            networkConfig.ConfigureWithoutCarrier = true;
          })
          (lib.nameValuePair "40-${net.name}" {
            vrf = [ net.vrf ];
            address = [ net.router.address ] ++ lib.optional (net.router.address6 != null) net.router.address6;
            networkConfig = {
              DHCPServer = net.router.dhcp;
              ConfigureWithoutCarrier = true;
              IPv6SendRA = net.router.address6 != null;
              IPv6AcceptRA = false;
            };
            dhcpServerConfig = lib.mkIf net.router.dhcp {
              EmitRouter = true;
              Router = lib.head (lib.splitString "/" net.router.address);
              EmitDNS = false;
            };
            ipv6SendRAConfig = {
              EmitDNS = false;
              EmitDomains = false;
            };
            ipv6Prefixes = lib.optional (net.router.address6 != null) { Prefix = net.router.address6; };
          })
        ]) networks
      );

      networking.nftables.ruleset = lib.concatMapStrings (
        net:
        offNet {
          table = "router-${net.name}";
          device = net.name;
          tproxyIn = net.vrf;
          subnets4 = [ net.router.address ];
          subnets6 = lib.optional (net.router.address6 != null) net.router.address6;
          inherit (net) tcp udp;
        }
      ) networks;

      systemd.services = lib.listToAttrs (
        map (
          net:
          lib.nameValuePair "vivarium-relay-${net.name}" (
            lib.mkMerge [
              (relayService {
                inherit (net) tcp udp;
                egress = toString net.egress;
                # No forwards reach a router network's guests.
                inward = "none";
              })
              (inVrf net.vrf)
              { description = "The router's relay for network ${net.name}"; }
            ]
          )
        ) networks
      );
    })
    # An uplink segment takes precedence: `MachineSpec.vsock` reads the same.
    (lib.mkIf (config.vivarium.uplink.proxy == "vsock" && config.vivarium.uplink.segment == null) {
      vivarium.launch.config.egress = "${egress}/bin/egress";

      # passt's addresses, fixed: nothing in the namespace serves DHCP.
      networking.interfaces.vec0 = {
        useDHCP = lib.mkForce false;
        ipv4.addresses = [
          {
            address = uplink.address 0;
            inherit (uplink) prefixLength;
          }
        ];
        ipv6.addresses = [
          {
            address = uplink.address6 0;
            prefixLength = 64;
          }
        ];
      };
      networking.defaultGateway = {
        address = gateway;
        interface = "vec0";
      };
      networking.defaultGateway6 = {
        address = gateway6;
        interface = "vec0";
      };
      systemd.network.networks."40-vec0".networkConfig.IPv6DuplicateAddressDetection = 0;

      systemd.services.vivarium-uplink = {
        description = "The uplink's namespace and veth pair";
        wantedBy = [ "multi-user.target" ];
        before = [
          "network-pre.target"
          "systemd-networkd.service"
        ];
        wants = [ "network-pre.target" ];
        path = [
          pkgs.iproute2
          pkgs.nftables
          pkgs.procps
          pkgs.util-linux
        ];
        serviceConfig = {
          Type = "oneshot";
          RemainAfterExit = true;
        };
        # Mark 1 is local here as on the router, and the relay answers
        # echo requests with the host's replies. Not networkd's: it does
        # not reach into another namespace.
        script = ''
          ip netns delete ${netns} 2>/dev/null || true
          ip netns add ${netns}
          ip link add vec0 mtu ${toString config.vivarium.mtu} type veth \
            peer name ${bridge} mtu ${toString config.vivarium.mtu} netns ${netns}
          # Not `ip -n` or `ip netns exec`: both mount a new /sys, which a
          # container may not do.
          nsenter --net=/run/netns/${netns} ${pkgs.runtimeShell} -e -c '
            ip link set lo up
            ip link set ${bridge} up
            ip address add ${gateway}/24 dev ${bridge}
            ip address add ${dns}/24 dev ${bridge}
            ip address add ${gateway6}/64 dev ${bridge} nodad
            ip rule add fwmark 1 table 100
            ip -6 rule add fwmark 1 table 100
            ip route add local 0.0.0.0/0 dev lo table 100
            ip -6 route add local ::/0 dev lo table 100
            sysctl -q -w net.ipv4.icmp_echo_ignore_all=1 net.ipv6.icmp.echo_ignore_all=1
            nft -f ${pkgs.writeText "uplink.nft" (ruleset bridge)}
          '
        '';
        preStop = "ip netns delete ${netns}";
      };

      systemd.services.vivarium-relay = lib.mkMerge [
        (relayService {
          tcp = 9000;
          udp = 9001;
          inherit (ends) inward;
        })
        {
          description = "The guest's relay to the host";
          after = [ "vivarium-uplink.service" ];
          requires = [ "vivarium-uplink.service" ];
          serviceConfig.NetworkNamespacePath = "/run/netns/${netns}";
        }
      ];
    })
  ];
}

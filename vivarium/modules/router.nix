/*
  The router guest (nixidae/nixidae#1404, docs/design/router-lab.md).

  A run with `router.enable` has one: every other guest's uplink (`vec0`)
  is a segment of its own to this guest, and the router bridges them into
  10.0.2.0/24. Each guest leases its own address by its uplink MAC. TCP,
  UDP and ICMP echo to anywhere off the bridge go to `relay`, which
  carries each flow over vsock to the egress on the host.

  A guest with `vivarium.uplink.proxy = "vsock"` is its own router: the
  same ruleset and relay in a network namespace, behind a veth pair whose
  guest end is `vec0`. AF_VSOCK works from that namespace: the kernel's
  `net.vsock.ns_mode` is `global`.
*/
{
  config,
  lib,
  pkgs,
  hostPkgs,
  nodes,
  ...
}:
let
  cfg = config.vivarium.router;
  inherit (lib) mkOption types;

  bridge = "uplink";
  subnet = "10.0.2.0/24";
  gateway = "10.0.2.2";
  dns = "10.0.2.3";
  # A ULA prefix: the egress makes every connection, so the guests'
  # addresses never leave the run. glibc prefers IPv4 to a ULA source.
  prefix6 = "fd00:10:0:2::";
  gateway6 = "${prefix6}2";

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

  /*
    Everything the guests send off the bridge goes to the relay. TCP by
    `redirect`, so the relay reads SO_ORIGINAL_DST; UDP by tproxy, and
    ICMP echo by mark 1 to a raw socket. The bridge's own traffic --
    DHCP, broadcast, multicast, neighbour discovery, a guest to a guest --
    stays. IPv6 has the same rules, with no DNS address of its own.
  */
  ruleset = ''
    define onlink = { ${subnet}, 224.0.0.0/4, 255.255.255.255 }
    table ip router {
      chain prerouting {
        type nat hook prerouting priority dstnat;
        iifname "${bridge}" ip daddr != $onlink meta l4proto tcp redirect to :9000
        iifname "${bridge}" ip daddr ${gateway} meta l4proto tcp redirect to :9000
        iifname "${bridge}" ip daddr ${dns} tcp dport 53 redirect to :9000
      }
      chain mangle {
        type filter hook prerouting priority mangle;
        iifname "${bridge}" ip daddr != $onlink meta l4proto udp meta mark set 1 tproxy to :9001
        iifname "${bridge}" ip daddr ${gateway} udp dport != 67 meta mark set 1 tproxy to :9001
        iifname "${bridge}" ip daddr ${dns} udp dport 53 meta mark set 1 tproxy to :9001
        iifname "${bridge}" ip daddr != $onlink icmp type echo-request meta mark set 1
        iifname "${bridge}" ip daddr ${gateway} icmp type echo-request meta mark set 1
      }
    }
    define onlink6 = { ${prefix6}/64, fe80::/10, ff00::/8 }
    table ip6 router {
      chain prerouting {
        type nat hook prerouting priority dstnat;
        iifname "${bridge}" ip6 daddr != $onlink6 meta l4proto tcp redirect to :9000
        iifname "${bridge}" ip6 daddr ${gateway6} meta l4proto tcp redirect to :9000
      }
      chain mangle {
        type filter hook prerouting priority mangle;
        iifname "${bridge}" ip6 daddr != $onlink6 meta l4proto udp meta mark set 1 tproxy to :9001
        iifname "${bridge}" ip6 daddr ${gateway6} meta l4proto udp meta mark set 1 tproxy to :9001
        iifname "${bridge}" ip6 daddr != $onlink6 icmpv6 type echo-request meta mark set 1
        iifname "${bridge}" ip6 daddr ${gateway6} icmpv6 type echo-request meta mark set 1
      }
    }
  '';

  relayService = {
    wantedBy = [ "multi-user.target" ];
    environment = {
      # E8: a 4 MiB vsock window; the yield after a short read.
      RELAY_VSOCK_BUFFER = "4194304";
      RELAY_YIELD = "1";
    };
    serviceConfig = {
      ExecStart = "${relay}/bin/relay 9000 5000 9001 5001";
      Restart = "always";
    };
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
      default = index: "10.0.2.${toString (15 + index)}";
      description = "A routed guest's uplink address, by its `vivarium.index`.";
    };
    address6 = mkOption {
      type = types.functionTo types.str;
      internal = true;
      readOnly = true;
      default = index: "${prefix6}${toString (15 + index)}";
      description = ''
        A routed guest's IPv6 uplink address, by its `vivarium.index`: the
        router's prefix and the digits of its IPv4 address's last octet.
      '';
    };
  };

  config = lib.mkMerge [
    # A guest behind the router: its address from the router's
    # advertisement, the host part fixed by its index.
    (lib.mkIf (config.vivarium.uplink.proxy == "router") {
      systemd.network.networks."40-vec0".ipv6AcceptRAConfig.Token = "static:::${
        toString (15 + config.vivarium.index)
      }";
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
      # 256M on krun OOM-kills the agent at boot, and on QEMU under load,
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
      # and no vec0 (`router` in its spec).
      vivarium.uplink.proxy = "passt";
      # Its egress binds nothing for it: no ssh, and nothing to reach.
      vivarium.forward = lib.mkForce [ ];
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
          # 10.0.2.(15 + index) stays below the pool at .215.
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

      systemd.network.netdevs."20-${bridge}".netdevConfig = {
        Kind = "bridge";
        Name = bridge;
        MTUBytes = toString config.vivarium.mtu;
      };
      systemd.network.networks = {
        "40-${bridge}" = {
          matchConfig.Name = bridge;
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
          # The guests' resolver is 10.0.2.3; IPv6 carries no DNS.
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
        inherit ruleset;
      };

      systemd.services.vivarium-relay = lib.recursiveUpdate relayService {
        description = "The router's relay to the host";
        after = [ "nftables.service" ];
      };
    })
    # An uplink segment takes precedence: `MachineSpec.vsock` reads the same.
    (lib.mkIf (config.vivarium.uplink.proxy == "vsock" && config.vivarium.uplink.segment == null) {
      vivarium.launch.config.egress = "${egress}/bin/egress";
      assertions = [
        {
          assertion = lib.elem config.vivarium.backend [
            "uml"
            "qemu"
            "krun"
          ];
          message = "a vsock uplink needs UML, QEMU or krun; this guest is ${config.vivarium.backend}.";
        }
      ];

      # passt's addresses, fixed: nothing in the namespace serves DHCP.
      networking.interfaces.vec0 = {
        useDHCP = lib.mkForce false;
        ipv4.addresses = [
          {
            address = "10.0.2.15";
            prefixLength = 24;
          }
        ];
        ipv6.addresses = [
          {
            address = "${prefix6}15";
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
          ip -n ${netns} link set lo up
          ip -n ${netns} link set ${bridge} up
          ip -n ${netns} address add ${gateway}/24 dev ${bridge}
          ip -n ${netns} address add ${dns}/24 dev ${bridge}
          ip -n ${netns} address add ${gateway6}/64 dev ${bridge} nodad
          ip -n ${netns} rule add fwmark 1 table 100
          ip -n ${netns} -6 rule add fwmark 1 table 100
          ip -n ${netns} route add local 0.0.0.0/0 dev lo table 100
          ip -n ${netns} -6 route add local ::/0 dev lo table 100
          ip netns exec ${netns} sysctl -q -w net.ipv4.icmp_echo_ignore_all=1 net.ipv6.icmp.echo_ignore_all=1
          ip netns exec ${netns} nft -f ${pkgs.writeText "uplink.nft" ruleset}
        '';
        preStop = "ip netns delete ${netns}";
      };

      systemd.services.vivarium-relay = lib.recursiveUpdate relayService {
        description = "The guest's relay to the host";
        after = [ "vivarium-uplink.service" ];
        requires = [ "vivarium-uplink.service" ];
        serviceConfig.NetworkNamespacePath = "/run/netns/${netns}";
      };
    })
  ];
}

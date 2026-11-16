/*
  The router guest (nixidae/nixidae#1404, docs/design/router-lab.md).

  A run with `router.enable` has one: every other guest's uplink (`vec0`)
  is a segment of its own to this guest, and the router bridges them into
  10.0.2.0/24. Each guest leases its own address by its uplink MAC. TCP,
  UDP and ICMP echo to anywhere off the bridge go to `relay`, which
  carries each flow over vsock to the egress on the host.
*/
{
  config,
  lib,
  pkgs,
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
  egress = pkgs.callPackage ../pkgs/vivarium-egress/package.nix { vhostUser = true; };

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
    })
    (lib.mkIf cfg.enable {
      # 256M on krun OOM-kills the agent at boot; UML boots in it.
      vivarium.memory = lib.mkDefault (if config.vivarium.backend == "krun" then "512M" else "256M");
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
          # Both reach the egress over vsock: UML through the egress's own
          # vhost-user device, krun through libkrun's.
          assertion = lib.elem config.vivarium.backend [
            "uml"
            "krun"
          ];
          message = "the router guest runs on UML or krun; it is ${config.vivarium.backend}.";
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

      systemd.services.vivarium-relay = {
        description = "The router's relay to the host";
        wantedBy = [ "multi-user.target" ];
        after = [ "nftables.service" ];
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
    })
  ];
}

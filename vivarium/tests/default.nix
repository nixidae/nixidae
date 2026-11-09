# The tests. Each directory here with a `default.nix` is one check,
# `checks.<directory>`; its file is a function of the names below.
{
  pkgs,
  lib,
  sources,
  nixvenv,
  vivarium,
  k8sImages,
}:
let
  inherit (vivarium)
    mkNode
    mkTest
    fromNixosTest
    runner
    session
    k8s
    cli
    typeCheck
    images
    ;

  # A host on a router's LAN: one interface, a route to the far LAN.
  lanHost = segment: address: gateway: far: {
    vivarium.interfaces.${segment} = {
      inherit segment;
      addresses = [ "${address}/24" ];
    };
    networking.interfaces.${segment}.ipv4.routes = [
      {
        address = far;
        prefixLength = 24;
        via = gateway;
      }
    ];
  };

  /*
    FRR in AS 65002 on 10.0.0.2/30 (segment "core"), announcing
    10.2.0.0/24 (segment "east", 10.2.0.1) to a peer at 10.0.0.1, AS 65001.
    The far side of the routeros-bgp and vyos-bgp labs.
  */
  frrPeer = {
    vivarium.interfaces = {
      core = {
        segment = "core";
        addresses = [ "10.0.0.2/30" ];
      };
      east = {
        segment = "east";
        addresses = [ "10.2.0.1/24" ];
      };
    };
    boot.kernel.sysctl."net.ipv4.ip_forward" = 1;
    networking.firewall.allowedTCPPorts = [ 179 ];
    services.frr = {
      bgpd.enable = true;
      config = ''
        frr defaults traditional
        router bgp 65002
         bgp router-id 10.0.0.2
         no bgp ebgp-requires-policy
         neighbor 10.0.0.1 remote-as 65001
         neighbor 10.0.0.1 timers 1 3
         neighbor 10.0.0.1 timers connect 1
         address-family ipv4 unicast
          network 10.2.0.0/24
         exit-address-family
        exit
      '';
    };
  };

  # A NixOS "services" guest for the out-of-band management network
  # (nixidae/nixidae#958): Kea hands out leases on `segment` from a pool,
  # and the guest holds the first address. A real run provisions foreign
  # guests from here (ssh/api), so this guest is also the jump host.
  #
  # `gateway` makes it the OOB gateway too: it routes the segment out
  # through its passt uplink (`vec0`) and masquerades, advertises itself
  # as the DHCP default route, and `forwardPorts` (nixpkgs' `networking.nat`
  # shape) DNATs a port arriving over passt to an address on the segment --
  # the single forwarding point host->OOB-guest access goes through.
  # `reservations` (Kea's subnet shape) pins a client's address outside the
  # pool, so a DNAT target is stable. Both default off, so a plain services
  # guest keeps its current configuration unchanged.
  servicesNode =
    {
      segment,
      gateway ? false,
      reservations ? [ ],
      forwardPorts ? [ ],
    }:
    {
      vivarium.interfaces.${segment} = {
        inherit segment;
        addresses = [ "10.42.0.1/24" ];
      };
      networking.nat = lib.mkIf gateway {
        enable = true;
        externalInterface = "vec0";
        internalInterfaces = [ segment ];
        inherit forwardPorts;
      };
      services.kea.dhcp4 = {
        enable = true;
        settings = {
          valid-lifetime = 3600;
          lease-database = {
            type = "memfile";
            persist = true;
            name = "/var/lib/kea/dhcp4.leases";
          };
          interfaces-config = {
            dhcp-socket-type = "raw";
            interfaces = [ segment ];
          };
          subnet4 = [
            (
              {
                id = 1;
                subnet = "10.42.0.0/24";
                # Bound to the interface, so Kea picks the subnet by the link a
                # request arrives on and not by an address that networkd may
                # not have put on yet.
                interface = segment;
                pools = [ { pool = "10.42.0.100 - 10.42.0.150"; } ];
              }
              // lib.optionalAttrs (reservations != [ ]) { inherit reservations; }
              // lib.optionalAttrs gateway {
                option-data = [
                  {
                    name = "routers";
                    data = "10.42.0.1";
                  }
                ];
              }
            )
          ];
        };
      };
    };

  # Two guests on one segment, addressed statically.
  pair = network: {
    server = {
      vivarium.lan = {
        inherit network;
        address = "192.168.99.2/24";
      };
    };
    client = {
      vivarium.lan = {
        inherit network;
        address = "192.168.99.3/24";
      };
    };
  };

  # The pair above, each running an iperf3 server. Shared by `iperf` and
  # `iperf-qemu`, so the two measure the same guests on the same segment
  # and only the machine underneath differs.
  iperfNodes = lib.mapAttrs (_: node: {
    imports = [
      node
      ../modules/iperf3.nix
    ];
    services.iperf3-server.enable = true;
  }) (pair "lan");

  # `containerd`'s questions with kata as the only extra handler. The node
  # holds kata's VM too, 2 GiB by kata's default; guest RAM is sparse, so
  # the ceiling costs nothing until touched.
  kataTest =
    cri:
    mkTest {
      name = "kata-${cri}";
      backend = "qemu";
      vivarium.recipes.kubernetes.enable = true;
      phases.test = {
        script = ./containerd.py;
        after = [ "boot" ];
      };
      nodes.node = {
        services.vivarium-k8s = {
          enable = true;
          role = "worker";
          inherit cri;
          runtimes = [ "kata" ];
        };
        vivarium = {
          nestedVirtualization = true;
          memory = "4096M";
          diskSize = 2048;
          lan = {
            network = "kata";
            address = "10.106.0.1/24";
          };
        };
      };
      settings = {
        inherit (k8sImages) sandboxImage entrypoints;
        kubernetesVersion = pkgs.kubernetes.version;
      };
    };

  /*
    Three guests running kubeadm: one control plane, two workers.

    The control plane carries etcd and the API server and needs the
    memory to prove it; the workers only run kubelet, kube-proxy and
    whatever the test schedules.  Everything else about the cluster
    -- who joins whom, which pod subnet each node got -- is worked
    out at run time in tests/k8s.py, because only the test can see
    all three at once.
  */
  k8sNodes =
    let
      node = index: role: {
        imports = [ ../modules/k8s.nix ];
        services.vivarium-k8s = {
          enable = true;
          inherit role;
          # One each, so the claim tests/k8s.py makes has somewhere to
          # land whichever worker the scheduler picks.  The control plane
          # is tainted and gets one anyway: a taint is not a guarantee.
          persistentVolumes = 1;
        };
        vivarium = {
          memory = if role == "control-plane" then "2560M" else "1280M";
          # The images are symlinks into the host's store, so this
          # only has to hold containerd's state, the kubelet's, and
          # the logs -- not a copy of Kubernetes.
          diskSize = 2048;
          lan = {
            network = "k8s";
            address = "10.100.0.${toString index}/24";
          };
        };
      };
    in
    {
      cp = node 1 "control-plane";
      worker1 = node 2 "worker";
      worker2 = node 3 "worker";
    };

  # Where the tests read the cluster's settings from, rather than
  # repeating subnets and image names in Python.
  k8sConfig = (mkNode k8sNodes.cp).config;
  demo = mkNode {
    vivarium.memory = "512M";
    # A guest you drive by hand rather than from a test: give it a
    # host address to itself with everything on it forwarded, so
    # whatever you start in there is reachable without having said
    # so in advance.  Tests keep the narrow default; three guests
    # holding 36000 sockets each is not what a builder is for.
    vivarium.forward = [ { ports = "all"; } ];
    environment.systemPackages = [ pkgs.speedtest-cli ];
  };

  /*
    Can Nix inside a guest use the host's whole store?

    Deliberately not in `tests`, so it is neither a check nor built by
    CI: the lower layer of the guest's store is the host's Nix database,
    and a build sandbox has no `/nix/var` in it at all.  Run it by hand
    -- tests/store.py says how at its head.
  */
  store = mkTest {
    name = "store";
    phases.test = {
      script = ./store.py;
      after = [ "boot" ];
    };
    # A path handed to the test the way a caller hands one over, and
    # nothing else names it. The runner registers it with the guest
    # (`lateClosure`), which is the half of the guest's store that does not
    # come from the host's database -- and cannot, because a path this
    # fresh is still in the host's write-ahead log.
    settings.probe = "${pkgs.runCommand "vivarium-store-probe" { } "echo settings > $out"}";
    nodes.node =
      { config, settings, ... }:
      {
        vivarium = {
          lateClosure = [ settings.probe ];
          hostStore.enable = true;
          memory = "1024M";
        };
        environment.systemPackages = [ config.nix.package ];
      };
  };

  /*
    A cluster the way a real one comes up: images pulled, nothing patched.

    `k8s` builds every image from nixpkgs and imports it, which is what
    makes a cluster possible inside a build sandbox -- and what makes the
    node unlike other nodes, because the store has to be mounted into
    every container that runs one of those images. Anything whose job is
    to put a store into a pod passes there with its subject switched off.

    So this one turns all of it off. Not in `tests`, for the same reason
    `store` is not: it pulls from registry.k8s.io, and a build sandbox has
    no network.

    10.104, and not the 10.100 `k8s` uses: unsandboxed, a guest routes for
    real, and this host has a WireGuard interface on 10.100.0.1/24.
  */
  k8s-pull = mkTest {
    name = "k8s-pull";
    backend = "qemu";
    phases.test = {
      script = ./pull.py;
      after = [ "boot" ];
    };
    vivarium.recipes.kubernetes.enable = true;
    nodes.cp = {
      services.vivarium-k8s = {
        enable = true;
        role = "control-plane";
        images = "pull";
      };
      vivarium = {
        memory = "4096M";
        diskSize = 8192;
        cpus = 4;
        lan = {
          network = "k8s-pull";
          address = "10.104.0.1/24";
        };
      };
    };
  };

  /*
    Does `--offline` give a guest the network a sandbox gives it?

    Deliberately not in `tests`, and it cannot be: a sandboxed run has no
    network whatever the flag says, so the check would pass without the
    flag doing anything. The only honest proof is by hand, on a connected
    host, comparing the two:

        nix run --file . internal.uplink.driver -- --out ./out
        nix run --file . internal.uplink.driver -- --out ./out --offline

    and compare both with the build, `nix build --file . internal.uplink`.
    Measured on 2026-10-26, UML backend, dynhetz and sg1-n3:

        plain      tcp4 reachable, tcp6 reachable, the host's IPv6
                   prefix on vec0, ping answered, dns answered
        --offline  tcp4 no route, tcp6 no route, link-local only,
                   no ping, dns no answer
        build      the same as --offline, line for line

    `--offline` runs the session in a network namespace of its own with
    only lo, as a build sandbox does (`vivarium/namespace.py`), so passt
    takes its local mode in both and they cannot drift apart. passt stays:
    the guest keeps its address, its DHCP lease and the host's way in, and
    a test reaching an API server through a forward fails, if it fails,
    for the reason being reproduced. Binding passt's outbound to loopback
    is not enough on its own: it binds IPv4 only, and IPv6 got out, with
    the host's prefix copied onto vec0 (nixidae#1094).
  */
  uplink = mkTest {
    name = "uplink";
    nodes.one = { };
    phases = {
      boot.script = ./phases/boot.py;
      uplink = {
        script = ./phases/uplink.py;
        after = [ "boot" ];
      };
    };
  };

  /*
    Does a guest's incremental write reach the host incrementally?

    The journal stream rests on this answer. Measured on both backends
    (2026-09-24): the host sees each line the moment the guest writes
    it, `(7,7) (14,14) ... (42,42)` guest/host bytes, and `sync`
    changes nothing. hostfs and virtiofs are both write-through here.

    By hand:

        nix run --file . internal.incr.driver -- --out ./out
  */
  incr = mkTest {
    name = "incr";
    nodes.one = { };
    phases.incr = {
      script = ./phases/incr.py;
      after = [ "boot" ];
    };
  };

  scope = {
    inherit
      pkgs
      lib
      sources
      nixvenv
      vivarium
      mkNode
      mkTest
      fromNixosTest
      runner
      session
      k8s
      cli
      typeCheck
      images
      lanHost
      frrPeer
      servicesNode
      pair
      iperfNodes
      kataTest
      k8sNodes
      k8sConfig
      k8sImages
      demo
      store
      k8s-pull
      uplink
      incr
      ;
  };
  isTest = name: type: type == "directory" && builtins.pathExists (./. + "/${name}/default.nix");
in
{
  checks = lib.mapAttrs (name: _: import (./. + "/${name}") scope) (
    lib.filterAttrs isTest (builtins.readDir ./.)
  );
  internal = {
    inherit
      demo
      store
      k8s-pull
      uplink
      incr
      ;
  };
  inherit k8sConfig;
}

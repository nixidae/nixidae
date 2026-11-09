# Everything this repository builds.
#
# This is the way in, and `flake.nix` is a second door that calls it.
#
#     nix build --file . checks.lan          # two guests on a segment
#     nix build --file . checks.containerd   # one guest running a container
#     nix build --file . checks.k8s          # three guests, a kubeadm cluster
#
# **One dependency, and it is nixpkgs.** A guest is an ordinary NixOS
# configuration and a kernel built from the host's own package set; nothing
# here needs anything else. So this takes a package set and nothing else, and
# a caller that has one -- a flake, another repository, nixidae -- passes it
# in.
#
# A caller with none gets nixidae's, the way every other project in nixidae
# does: `nix/sources.nix` asks nixidae, inside or outside. `<nixpkgs>` would
# build against whatever the machine's NIX_PATH holds -- nothing on a CI
# runner, and something other than nixidae's pin on a developer's. Store
# paths then agree with nobody, so no cache can serve them.
#
# `mkNode` and `mkTest` come out of `lib.nix` and are re-exported here, so a
# caller with its own guests and its own script needs nothing else.
{
  sources ? import ./nix/sources.nix,
  # Another system's derivations build on a builder of it.
  system ? builtins.currentSystem,
  pkgs ? import sources.nixpkgs { inherit system; },
}:
let
  inherit (pkgs) lib;
  inherit ((import sources.nixvenv { inherit pkgs; }).lib) nixvenv;

  vivarium = import ./lib.nix { inherit pkgs lib sources; };
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
      ./modules/iperf3.nix
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
        script = ./tests/containerd.py;
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
        imports = [ ./modules/k8s.nix ];
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
  k8sImages = pkgs.callPackage ./modules/k8s-images.nix { };

  # The GitHub Actions workflows, as values. nixidae renders them.
  ci = import ./ci { inherit lib sources; };

  optionsReference = import ./nix/shared/options-reference.nix { inherit lib pkgs; };
  docs = pkgs.python3Packages.callPackage ./nix/docs.nix {
    inherit nixvenv;
    # What `mkTest` evaluates, without a guest.
    testOptions = optionsReference {
      title = "Test options";
      inherit
        (lib.evalModules {
          modules = [
            ./modules/run.nix
            ./modules/recipes
          ];
          specialArgs = { inherit pkgs; };
        })
        options
        ;
      prefixes = [
        "backend"
        "defaults"
        "interactive"
        "knobs"
        "name"
        "nodes"
        "phases"
        "pythonPath"
        "settings"
        "vivarium."
      ];
      root = ./.;
      repository = "nixidae/vivarium";
    };
    guestOptions = optionsReference {
      title = "Guest options";
      inherit (mkNode { }) options;
      prefixes = [ "vivarium." ];
      root = ./.;
      repository = "nixidae/vivarium";
    };
  };

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
      script = ./tests/store.py;
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
      script = ./tests/pull.py;
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
      boot.script = ./tests/phases/boot.py;
      uplink = {
        script = ./tests/phases/uplink.py;
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
      script = ./tests/phases/incr.py;
      after = [ "boot" ];
    };
  };

  tests = {
    # Do the guests boot, see each other on vec1, and answer the host?
    lan = mkTest {
      name = "lan";
      phases.test = {
        script = ./tests/lan.py;
        after = [ "boot" ];
      };
      nodes = pair "lan";
    };

    /*
      An appliance: RouterOS CHR booted from MikroTik's image, beside a
      NixOS guest on one segment. Needs /dev/kvm, as QEMU does.
    */
    appliance = mkTest {
      name = "appliance";
      phases.test = {
        script = ./tests/appliance.py;
        after = [ "boot" ];
      };
      nodes.router = {
        vivarium.backend = "qemu";
        imports = [ vivarium.kinds.appliance ];
        vivarium.appliance = vivarium.images.routeros-chr-7_24_5;
        vivarium.forward = [ { ports = [ 80 ]; } ];
        vivarium.interfaces.fabric.segment = "fabric";
      };
      nodes.peer.vivarium.interfaces.fabric.segment = "fabric";
    };

    /*
      VyOS booted with a config written in Nix, beside a NixOS peer.
      Needs /dev/kvm.
    */
    vyos = mkTest {
      name = "vyos";
      phases.test = {
        script = ./tests/vyos.py;
        after = [ "boot" ];
      };
      nodes.edge = { config, ... }: {
        vivarium.backend = "qemu";
        vivarium.memory = "1536M";
        imports = [ vivarium.kinds.appliance ];
        vivarium.appliance = vivarium.images.vyos-2026_10_26 ''
          firewall {
              ipv4 {
                  input {
                      filter {
                          rule 10 {
                              action "drop"
                              protocol "icmp"
                              state "invalid"
                          }
                      }
                  }
              }
          }
          interfaces {
              ethernet eth1 {
                  hw-id "${(lib.head config.vivarium.nics).mac}"
                  address "192.0.2.1/24"
                  address "2001:db8::1/64"
              }
              loopback lo {
              }
          }
          system {
              console {
                  device ttyS0 {
                      kernel
                      speed "115200"
                  }
              }
              host-name "edge"
              login {
                  user vyos {
                      authentication {
                          plaintext-password "vyos"
                      }
                  }
              }
          }
        '';
        vivarium.interfaces.lan.segment = "lan";
      };
      nodes.peer.vivarium.interfaces.lan = {
        segment = "lan";
        addresses = [
          "192.0.2.2/24"
          "2001:db8::2/64"
        ];
      };
    };

    /*
      Can the host reach a service in a guest?

      The only test that connects inwards.  One guest with every
      port forwarded, and a web server started long after passt
      stopped accepting arguments -- which is the case that cannot
      be checked any other way, since passt's forwards are fixed for
      its lifetime.
    */
    forward = mkTest {
      name = "forward";
      phases.test = {
        script = ./tests/forward.py;
        after = [ "boot" ];
      };
      nodes.node = {
        vivarium.forward = [ { ports = "all"; } ];
        environment.systemPackages = [ pkgs.python3 ];
      };
    };

    /*
      Does a QEMU guest come back from its own reboot, on the system its
      bootloader names? `next` changes the kernel parameters and the
      initrd, which only a new boot picks up. `two` must still reach it
      over their segment. `.krun` asks the same of libkrun guests. Not
      UML: a UML guest has no initrd, and its kernel is the process
      (nixidae/nixidae#1162).
    */
    reboot = mkTest (
      { systems, ... }:
      {
        name = "reboot";
        backend = "qemu";
        phases.test = {
          script = ./tests/reboot.py;
          after = [ "boot" ];
        };
        defaults =
          { settings, ... }:
          {
            vivarium.lateClosure = [ settings.next ];
          };
        nodes = lib.genAttrs [ "one" "two" ] (name: {
          vivarium.lan = {
            network = "reboot";
            address = "10.105.0.${if name == "one" then "1" else "2"}/24";
          };
        });
        settings.next = "${(systems.one.extendModules {
          modules = [
            {
              boot.kernelParams = [ "vivarium.reboot=next" ];
              boot.initrd.systemd.contents."/etc/vivarium-reboot".text = "next\n";
              environment.etc."vivarium-reboot".text = "next\n";
            }
          ];
        }).config.system.build.toplevel
        }";
      }
    );

    /*
      Does a guest on `vivarium.efi` boot through systemd-boot, and does
      systemd-boot fall back from a generation that never completes a
      boot? The same claims as nixpkgs' `systemd-boot.bootCounting`, on
      vivarium's firmware boot.

      `good` and `bad` differ from the booted system by one file each.
      `bad` also has a unit that `boot-complete.target` requires and that
      fails, so systemd-bless-boot never marks its entry good.
    */
    boot-counting = mkTest (
      { systems, ... }:
      let
        next =
          name: extra:
          "${(systems.one.extendModules {
            modules = [
              {
                environment.etc."vivarium-generation".text = "${name}\n";
              }
              extra
            ];
          }).config.system.build.toplevel
          }";
      in
      {
        name = "boot-counting";
        backend = "qemu";
        phases.test = {
          script = ./tests/boot-counting.py;
          after = [ "boot" ];
        };
        nodes.one =
          { settings, ... }:
          {
            vivarium.lateClosure = [
              settings.good
              settings.bad
            ];
            vivarium.efi = true;
            boot.loader.systemd-boot.bootCounting = {
              enable = true;
              tries = 2;
            };
          };
        settings = {
          good = next "good" { };
          bad = next "bad" {
            systemd.services.vivarium-bad-boot = {
              requiredBy = [ "boot-complete.target" ];
              before = [ "boot-complete.target" ];
              serviceConfig.Type = "oneshot";
              script = "exit 1";
            };
          };
        };
      }
    );

    /*
      Does what a guest writes reach the host, and who is left running?

      Two guests, because each one must get its own directory.
      `pkgs.util-linux` for `mountpoint`.
    */
    artifacts = mkTest {
      name = "artifacts";
      phases.test = {
        script = ./tests/artifacts.py;
        after = [ "boot" ];
      };
      nodes = lib.genAttrs [ "one" "two" ] (_: {
        environment.systemPackages = [ pkgs.util-linux ];
        users.users.tester.isNormalUser = true;
      });
    };

    /*
      Does a failure skip what depends on it, and nothing else?

      The claim the whole phase design rests on, against a booted guest.
      Four phases: `boot` passes, `cluster` fails on purpose, `check`
      needs `cluster` and must be skipped, `independent` needs only
      `boot` and must still run.

      Both neighbours get this wrong, which is why it is worth a test.
      nixpkgs' driver re-raises out of `subtest`, so `independent` would
      never run and a real bug in it would stay invisible behind an
      unrelated failure. pytest would run `check` anyway, against a
      cluster that was never built, and report a second failure that
      says nothing.

      The session derivation *fails* when a phase fails -- which is
      correct -- so this reads `phases.json` from the attempt instead.
    */
    phase-rules =
      let
        run = mkTest {
          name = "phase-rules";
          nodes.one = { };
          phases = {
            boot.script = ./tests/phases/boot.py;
            cluster = {
              script = ./tests/phases/cluster.py;
              after = [ "boot" ];
            };
            check = {
              script = ./tests/phases/check.py;
              after = [ "cluster" ];
            };
            independent = {
              script = ./tests/phases/independent.py;
              after = [ "boot" ];
            };
          };
        };
      in
      pkgs.runCommand "vivarium-check-phase-rules" { passthru.session = run; } ''
        ${pkgs.python3.interpreter} ${./tests/phase_rules.py} ${run.attempt}
        touch $out
      '';

    /*
      What is a run told from outside, and what is a check told instead?

      A knob is resolved while evaluating, so it can change what is built
      -- a phase order, a guest's memory, an image -- which nothing read
      at run time can do. The price is that setting one moves the
      derivation, and that is the trade this records rather than hides.

      The property under test is the half that keeps CI honest: inside a
      sandbox a knob always carries its declared default, because
      `builtins.getEnv` answers "" under a pure evaluation and an unset
      variable answers "" too. So an exported variable cannot make the
      check run something other than the check.

      Also asserts a knob is not ambient. Nothing in the guest's
      environment carries it; a phase hands it over or the guest never
      sees it, which keeps a guest's behaviour a function of its own
      configuration.
    */
    knobs = mkTest {
      name = "knobs";
      nodes.one = { };
      knobs.selection = {
        env = "VIVARIUM_SELECTION";
        default = "every-case";
        description = "Which cases to run; the default is all of them.";
      };
      phases = {
        boot.script = ./tests/phases/boot.py;
        knob = {
          script = ./tests/phases/knob.py;
          after = [ "boot" ];
        };
      };
    };

    # The run's and a guest's `python.packages` reach the phase and its
    # type check, in one venv.
    python-packages = mkTest {
      name = "python-packages";
      nodes.one.vivarium.python.packages = [ pkgs.python3Packages.humanize ];
      python.packages = [ pkgs.python3Packages.tomli-w ];
      phases = {
        boot.script = ./tests/phases/boot.py;
        imports = {
          script = ./tests/phases/python-packages.py;
          after = [ "boot" ];
        };
      };
    };

    /*
      Can a caller run one phase and leave the rest alone?

      `--only` is the fast door: boot once, do the one thing being worked
      on, and exit 0 when it passed. A developer who asked for one phase
      knows the rest did not run.

      The safety property is that only a *caller* can do this. The check
      passes no `--only`, so CI cannot go green by running a subset --
      and the session below proves it, because one of its phases fails on
      purpose and building it whole fails.

      `deselected` is therefore a different state from `skipped`. Skipped
      means nobody knows the answer and the run failed; deselected means
      nobody wanted it.
    */
    only-rules =
      let
        run = mkTest {
          name = "only";
          nodes.one = { };
          phases = {
            boot.script = ./tests/phases/boot.py;
            only = {
              script = ./tests/phases/only.py;
              after = [ "boot" ];
              # Carried through the spec; a pass says the limit is not hit.
              timeout = 300;
            };
            # Each fails if it ever runs, which is the point: `--only`
            # must not reach either, and building this session whole must
            # fail. `--only` takes what `only` runs after, never what runs
            # after it.
            never = {
              script = ./tests/phases/check.py;
              after = [ "boot" ];
            };
            later = {
              script = ./tests/phases/check.py;
              after = [ "only" ];
            };
          };
        };
      in
      pkgs.runCommand "vivarium-check-only" { passthru.session = run; } ''
        ${pkgs.python3.interpreter} ${./tests/checks}/only.py ${lib.getExe cli} ${lib.getExe run.driver} "$TMPDIR"
        touch $out
      '';

    /*
      Does `--only` boot only what its phases need?

      `check` runs on `a`, and `a` requires `b`. `other` runs on `c`, and
      `--only check` deselects it, so `c` never boots. The `boot` recipe
      is in the `after` closure and takes the guests of `check` alone.
      tests/checks/on_demand.py.
    */
    on-demand =
      let
        run = mkTest {
          name = "on-demand";
          nodes = {
            a.vivarium.requires = [ "b" ];
            b = { };
            c = { };
          };
          phases = {
            check = {
              script = ./tests/phases/on-demand.py;
              after = [ "boot" ];
              nodes = [ "a" ];
            };
            other = {
              script = ./tests/phases/check.py;
              after = [ "boot" ];
              nodes = [ "c" ];
            };
          };
        };
      in
      pkgs.runCommand "vivarium-check-on-demand" { passthru.session = run; } ''
        ${pkgs.python3.interpreter} ${./tests/checks}/on_demand.py ${lib.getExe cli} ${lib.getExe run.driver} "$TMPDIR"
        touch $out
      '';

    /*
      Does evidence get collected on the run that went wrong?

      `boot` is the standard library's recipe, which nothing here
      declares. `evidence` is ordered after a phase that fails on
      purpose, and `after` alone would skip it for exactly that reason.
      `always` is what separates "run me later" from "do not bother if
      that failed".

      Also checks the failure does not pass *through* `evidence`: `later`
      needs only `evidence`, so it still runs.
    */
    recipes =
      let
        run = mkTest {
          name = "recipes";
          nodes.one = { };
          phases = {
            cluster = {
              script = ./tests/phases/cluster.py;
              after = [ "boot" ];
            };
            evidence = {
              script = ./tests/phases/evidence.py;
              after = [ "cluster" ];
              always = true;
            };
            later = {
              script = ./tests/phases/independent.py;
              after = [ "evidence" ];
            };
          };
        };
      in
      pkgs.runCommand "vivarium-check-recipes" { passthru.session = run; } ''
        ${pkgs.python3.interpreter} ${./tests/checks}/recipes.py ${run.attempt}
        touch $out
      '';

    /*
      Does a guest's journal survive the guest?

      A unit logs a line, the phase waits until the line is in the
      host-side file while the guest still runs, and then kills the guest
      with SIGKILL -- no shutdown, nothing flushed. The line must be in
      `events.jsonl` afterwards as an event that carries the machine, the
      unit and the phase. That is the question an agent asks with `jq`,
      and the failure it has to answer for is the spectacular kind.
    */
    stream =
      let
        run = mkTest {
          name = "stream";
          nodes.one = { };
          phases.crash = {
            script = ./tests/phases/crash.py;
            after = [ "boot" ];
          };
        };
      in
      pkgs.runCommand "vivarium-check-stream" { passthru.session = run; } ''
        ${pkgs.python3.interpreter} ${./tests/checks}/stream.py ${run.attempt}
        touch $out
      '';

    /*
      Is a pytest phase pytest, against real guests?

      `tests/cases` uses what a test author reaches for: a guest as a
      fixture, an async fixture with a teardown, parametrize, a skip, and
      one assertion that fails on purpose. The phase fails, so this reads
      the attempt.

      The claims: each test is a JUnit case of its own, the failure
      message is pytest's rewritten assertion, the fixture's teardown ran
      on the guest, and a journal entry and a command both name the test
      that caused them.
    */
    pytest-phase =
      let
        run = mkTest {
          name = "pytest";
          nodes.one = { };
          phases.cases = {
            pytest.tests = ./tests/cases;
            after = [ "boot" ];
          };
        };
      in
      pkgs.runCommand "vivarium-check-pytest-phase" { passthru.session = run; } ''
        ${pkgs.python3.interpreter} ${./tests/checks}/pytest_phase.py ${run.attempt}
        touch $out
      '';

    /*
      One guest as a rootless container under crun (Area 8 of the design).

      `nix build --file . checks.container` needs a daemon with the `uid-range`
      feature; CI's `test-container` gets it from ghanix's
      `nix.install.uidRange`. There the guest has no uplink (no
      /dev/net/tun) and a read-only store, and the phase says so. By hand, `vivarium run checks.container`, it has both;
      the host needs subordinate ids and a cgroup it can delegate, and
      the runner says which is missing.
    */
    container = mkTest {
      name = "container";
      nodes.one.vivarium.backend = "container";
      phases.check = {
        script = ./tests/phases/container.py;
        after = [ "boot" ];
      };
    };

    # Can this sandbox run a container guest? Seconds, and every missing
    # piece named with its fix. `-tun` also asks for a tap device, which a
    # LAN needs: /dev/net in `extra-sandbox-paths`.
    container-probe = vivarium.containerProbe { };
    container-probe-tun = vivarium.containerProbe { tun = true; };

    # Two containers and a UML guest on one segment. By hand, as above.
    container-lan = mkTest {
      name = "container-lan";
      nodes =
        lib.mapAttrs
          (name: value: {
            vivarium = {
              backend = if name == "u" then "uml" else "container";
              lan = {
                network = "clan";
                address = "${value}/24";
              };
            };
          })
          {
            a = "10.56.0.1";
            b = "10.56.0.2";
            u = "10.56.0.3";
          };
      phases.reach = {
        script = ./tests/phases/container-lan.py;
        after = [ "boot" ];
      };
    };

    /*
      Several segments per guest, each interface named as declared, on all
      three backends: `r` (UML) routes nothing between `a` and `b`; `q`
      (QEMU) and `c` (a container) also share `ll`, with link-local
      addresses only.

      By hand: the QEMU guest needs /dev/kvm and the container a tap.
    */
    segments = mkTest {
      name = "segments";
      nodes.r.vivarium = {
        backend = "uml";
        interfaces = {
          left = {
            segment = "a";
            addresses = [
              "10.70.1.1/24"
              "fd70:1::1/64"
            ];
          };
          right = {
            segment = "b";
            addresses = [ "10.70.2.1/24" ];
          };
        };
      };
      nodes.q.vivarium = {
        backend = "qemu";
        interfaces = {
          up = {
            segment = "a";
            addresses = [
              "10.70.1.2/24"
              "fd70:1::2/64"
            ];
          };
          spare.segment = "ll";
        };
      };
      nodes.c.vivarium = {
        backend = "container";
        interfaces = {
          down = {
            segment = "b";
            addresses = [ "10.70.2.2/24" ];
          };
          side.segment = "ll";
        };
      };
      phases.reach = {
        script = ./tests/phases/segments.py;
        after = [ "boot" ];
      };
    };

    # BGP unnumbered, EVPN L2 and symmetric L3 on a CLOS of FRR guests (#23).
    frr-clos = import ./examples/frr-clos { inherit mkTest lib pkgs; };

    # strongSwan site-to-site with certificates: policy- and route-based
    # tunnels, eBGP over them, MTU, rekey, failover and impostors.
    ipsec = import ./examples/ipsec { inherit mkTest lib pkgs; };

    /*
      One run, both kinds of guest: a UML guest and a QEMU guest on one
      segment. UML for what is single-threaded and wants to cost the host
      little, QEMU for what wants the CPU. A node sets its own
      `vivarium.backend`; the run's `backend` is only the default.

      By hand: the QEMU guest needs /dev/kvm, which the session job in CI
      does not have.
    */
    mixed = mkTest {
      name = "mixed";
      nodes.small.vivarium = {
        backend = "uml";
        lan = {
          network = "mixed";
          address = "10.55.0.1/24";
        };
      };
      nodes.fast.vivarium = {
        backend = "qemu";
        lan = {
          network = "mixed";
          address = "10.55.0.2/24";
        };
      };
      phases.reach = {
        script = ./tests/phases/mixed.py;
        after = [ "boot" ];
      };
    };

    /*
      Can a QEMU guest run virtual machines of its own?

      By hand, not in CI: a GitHub runner is itself a VM, and its KVM
      does not nest a second time. Needs nesting on this host
      (`/sys/module/kvm_{intel,amd}/parameters/nested`).
    */
    nested = mkTest {
      name = "nested";
      backend = "qemu";
      nodes.nested = {
        vivarium.nestedVirtualization = true;
        environment.systemPackages = [ pkgs.python3 ];
      };
      nodes.plain = { };
      phases.kvm = {
        script = ./tests/phases/nested.py;
        after = [ "boot" ];
      };
    };

    /*
      Does openSUSE Leap boot under UML, with the agent in it?

      The image is SUSE's own cloud image; the agent, the journal stream
      and /artifacts come from a cloud-init seed. See
      modules/image-guest.nix.
    */
    suse = mkTest {
      name = "suse";
      # A closure the guest does not boot with, for `add_closure`. Not
      # plain python3: the agent runs on it, so the guest has it already.
      settings.python =
        let
          env = nixvenv {
            python = pkgs.python3;
            packages = ps: [ ps.requests ];
            executables = [ "python3" ];
          };
        in
        {
          closure = "${pkgs.closureInfo { rootPaths = [ env ]; }}";
          bin = "${env}/bin/python3";
        };
      nodes.suse = {
        imports = [ vivarium.kinds.image ];
        vivarium.image = images.opensuse-leap-16_0;
        vivarium.memory = "1024M";
      };
      phases.suse = {
        script = ./tests/phases/suse.py;
        after = [ "boot" ];
      };
    };

    /*
      Does VyOS boot under UML, from its ISO's squashfs, and apply its
      config.boot? See modules/vyos-guest.nix.
    */
    vyos-uml = mkTest {
      name = "vyos-uml";
      nodes.vyos = {
        imports = [ vivarium.kinds.vyos ];
        vivarium.vyos = images.vyos-rolling // {
          config = ''
            interfaces {
                dummy dum0 {
                    address "10.255.0.1/32"
                }
                loopback lo {
                }
            }
            system {
                host-name "vyos1"
            }
          '';
        };
        vivarium.memory = "1024M";
      };
      phases.vyos = {
        script = ./tests/phases/vyos.py;
        after = [ "boot" ];
      };
    };

    /*
      Does RouterOS boot under QEMU, apply its config disk, and answer
      through its serial console? See docs/design/console-guests.md.
    */
    routeros = mkTest {
      name = "routeros";
      backend = "qemu";
      nodes.r1.imports = [ vivarium.kinds.routeros ];
      nodes.r1.vivarium.routeros = images.routeros-chr // {
        config = ''
          /system identity set name=r1
          /ip address add address=10.255.0.1/32 interface=lo
        '';
      };
      phases.routeros = {
        script = ./tests/phases/routeros.py;
        after = [ "boot" ];
      };
    };

    /*
      Does SONiC VS boot under QEMU and answer a shell on its serial
      console? Milestone 1 for a Linux-shell console guest. See
      docs/design/console-guests.md.
    */
    sonic = mkTest {
      name = "sonic";
      backend = "qemu";
      nodes.sw1 = {
        imports = [ vivarium.kinds.sonic ];
        vivarium.sonic = images.sonic-vs;
      };
      phases.sonic = {
        script = ./tests/phases/sonic.py;
        after = [ "boot" ];
      };
    };

    /*
      The out-of-band management fabric (nixidae/nixidae#958): a NixOS
      services guest runs Kea DHCP4 on a segment, and a peer joins that
      segment as its uplink (`vec0`, not passt) and leases a management
      address there. This is how a foreign guest's management interface
      will reach the fabric.
    */
    oob = mkTest {
      name = "oob";
      backend = "qemu";
      nodes.svc = servicesNode { segment = "mgmt"; };
      nodes.client.vivarium.uplink.segment = "mgmt";
      phases.oob = {
        script = ./tests/phases/oob.py;
        after = [ "boot" ];
      };
    };

    /*
      The out-of-band fabric under UML (nixidae/nixidae#958): the same
      lease as `oob`, but both guests are UML processes. It proves a UML
      guest's uplink joins a management segment -- vec0 on a segment fd
      instead of the passt bridge -- which is what VyOS needs to reach the
      fabric, since VyOS runs under UML.
    */
    oob-uml = mkTest {
      name = "oob-uml";
      nodes.svc = servicesNode { segment = "mgmt"; };
      nodes.client.vivarium.uplink.segment = "mgmt";
      phases.oob = {
        script = ./tests/phases/oob.py;
        after = [ "boot" ];
      };
    };

    /*
      VyOS on the out-of-band fabric (nixidae/nixidae#958): VyOS's uplink
      (eth0) is on the management segment and leases from the services
      guest's Kea. The vyos-guest module renames vec0 to eth0 and DHCPs it
      when `uplink.segment` is set. VyOS reaches the fabric the real way,
      not over a static address nobody hands out.
    */
    vyos-oob = mkTest {
      name = "vyos-oob";
      nodes.svc = servicesNode { segment = "mgmt"; };
      nodes.vyos = {
        vivarium.uplink.segment = "mgmt";
        vivarium.memory = "1024M";
        imports = [ vivarium.kinds.vyos ];
        vivarium.vyos = images.vyos-rolling // {
          config = ''
            system {
                host-name "vyos-oob"
            }
          '';
        };
      };
      phases.oob = {
        script = ./tests/phases/vyos-oob.py;
        after = [ "boot" ];
      };
    };

    /*
      VyOS management VRF on the out-of-band fabric (nixidae/nixidae#958),
      the VyOS analogue of oob-routeros: eth0 (the uplink) leases in a
      `mgmt` VRF, and the dataplane (eth1) stays in the default table. The
      two routing tables are separate, so management and dataplane cannot
      reach each other -- the isolation sonic-vs could not instantiate,
      here with VyOS's real VRFs.
    */
    vyos-oob-vrf = mkTest {
      name = "vyos-oob-vrf";
      nodes.svc = servicesNode { segment = "mgmt"; };
      nodes.vyos = {
        vivarium.uplink.segment = "mgmt";
        vivarium.interfaces.eth1 = {
          segment = "data";
          addresses = [ "10.9.0.1/24" ];
        };
        vivarium.memory = "1024M";
        imports = [ vivarium.kinds.vyos ];
        vivarium.vyos = images.vyos-rolling // {
          uplinkVrf = "mgmt";
          config = ''
            system {
                host-name "vyos-vrf"
            }
          '';
        };
      };
      nodes.d1 = lanHost "data" "10.9.0.2" "10.9.0.1" "10.42.0.0";
      phases.vrf = {
        script = ./tests/phases/vyos-oob-vrf.py;
        after = [ "boot" ];
      };
    };

    /*
      Provision SONiC the real way (nixidae/nixidae#958): SONiC's eth0 is
      its uplink on the management segment and leases from the services
      guest's Kea; the services guest then reaches SONiC over SSH and
      applies configuration. No console beyond the boot login.
    */
    oob-sonic = mkTest {
      name = "oob-sonic";
      backend = "qemu";
      nodes.svc = {
        imports = [ (servicesNode { segment = "mgmt"; }) ];
        environment.systemPackages = [
          pkgs.sshpass
          pkgs.jq
        ];
      };
      nodes.sw1 = {
        imports = [ vivarium.kinds.sonic ];
        vivarium.sonic = images.sonic-vs;
        vivarium.uplink.segment = "mgmt";
      };
      phases.provision = {
        script = ./tests/phases/oob-sonic.py;
        after = [ "boot" ];
      };
      # The heavier path, on the same booted SONiC: push a whole
      # config_db.json and apply it with `config reload`.
      phases.reload = {
        script = ./tests/phases/oob-sonic-reload.py;
        after = [ "provision" ];
      };
    };

    /*
      RouterOS management VRF on the OOB fabric (nixidae/nixidae#958):
      ether1 (the uplink) is on the management segment, and the dataplane
      is a separate interface. Proves functional mgmt/dataplane isolation
      with a VRF, the model sonic-vs could not instantiate.
    */
    oob-routeros = mkTest {
      name = "oob-routeros";
      backend = "qemu";
      nodes.svc = servicesNode { segment = "mgmt"; };
      nodes.r1 = {
        imports = [ vivarium.kinds.routeros ];
        vivarium.routeros = images.routeros-chr // {
          # ether1 is the uplink on the management segment; put it in a
          # mgmt VRF. The dataplane interface stays in `main`, so the two
          # have separate routing tables.
          config = ''
            /ip vrf add name=mgmt interfaces=ether1
          '';
        };
        vivarium.uplink.segment = "mgmt";
        vivarium.interfaces.data = {
          segment = "data";
          addresses = [ "10.9.0.1/24" ];
        };
      };
      nodes.d1 = lanHost "data" "10.9.0.2" "10.9.0.1" "10.42.0.0";
      phases.vrf = {
        script = ./tests/phases/oob-routeros.py;
        after = [ "boot" ];
      };
    };

    /*
      The services guest as the OOB gateway (nixidae/nixidae#958): the
      single point the host reaches an out-of-band guest through. `sw`'s
      uplink is on the management segment and has no passt forward of its
      own; the host reaches a service in it through one passt forward on
      `svc`, which DNATs to `sw`. The reply only returns because Kea gives
      `sw` the default route back to `svc`, so this proves both halves at
      once.
    */
    oob-gateway = mkTest {
      name = "oob-gateway";
      backend = "qemu";
      nodes.svc =
        { nodes, ... }:
        {
          imports = [
            (servicesNode {
              segment = "mgmt";
              gateway = true;
              # Pin `sw` to 10.42.0.10, outside the pool, by its uplink MAC,
              # so the DNAT target is stable. Read from the node, so it stays
              # right whatever order the nodes evaluate in.
              reservations = [
                {
                  hw-address = nodes.sw.vivarium.uplinkMac;
                  ip-address = "10.42.0.10";
                }
              ];
              forwardPorts = [
                {
                  sourcePort = 8080;
                  destination = "10.42.0.10:8080";
                  proto = "tcp";
                }
              ];
            })
          ];
          # passt must forward 8080 for the DNAT to have anything to rewrite;
          # its forwards are fixed before boot.
          vivarium.forward = [ { ports = [ 8080 ]; } ];
        };
      nodes.sw = {
        vivarium.uplink.segment = "mgmt";
        environment.systemPackages = [ pkgs.python3 ];
      };
      phases.gateway = {
        script = ./tests/phases/oob-gateway.py;
        after = [ "boot" ];
      };
    };

    /*
      Two NixOS guests on two segments, routed through RouterOS. The
      router's interfaces carry their vivarium names and addresses.
    */
    routeros-route = mkTest {
      name = "routeros-route";
      backend = "qemu";
      nodes.r1 = {
        imports = [ vivarium.kinds.routeros ];
        vivarium.routeros = images.routeros-chr;
        vivarium.interfaces = {
          left = {
            segment = "left";
            addresses = [ "10.1.0.1/24" ];
          };
          right = {
            segment = "right";
            addresses = [ "10.2.0.1/24" ];
          };
        };
      };
      nodes.a = lanHost "left" "10.1.0.2" "10.1.0.1" "10.2.0.0";
      nodes.b = lanHost "right" "10.2.0.2" "10.2.0.1" "10.1.0.0";
      phases.route = {
        script = ./tests/phases/routeros-route.py;
        after = [ "boot" ];
      };
    };

    /*
      eBGP between RouterOS and FRR. Each announces its LAN, and a host on
      one LAN reaches a host on the other only through the routes BGP
      gave the routers: two hops.
    */
    routeros-bgp = mkTest {
      name = "routeros-bgp";
      backend = "qemu";
      nodes.r1 = {
        vivarium.interfaces = {
          core = {
            segment = "core";
            addresses = [ "10.0.0.1/30" ];
          };
          west = {
            segment = "west";
            addresses = [ "10.1.0.1/24" ];
          };
        };
        imports = [ vivarium.kinds.routeros ];
        vivarium.routeros = images.routeros-chr // {
          config = ''
            /ip firewall address-list add list=bgp-out address=10.1.0.0/24
            /routing bgp instance add name=r1 as=65001 router-id=10.0.0.1
            /routing bgp connection add name=f1 instance=r1 local.role=ebgp remote.address=10.0.0.2 remote.as=65002 output.network=bgp-out hold-time=3s keepalive-time=1s
          '';
        };
      };
      nodes.f1 = frrPeer;
      nodes.a = lanHost "west" "10.1.0.2" "10.1.0.1" "10.2.0.0";
      nodes.b = lanHost "east" "10.2.0.2" "10.2.0.1" "10.1.0.0";
      phases.bgp = {
        script = ./tests/phases/routeros-bgp.py;
        after = [ "boot" ];
      };
    };

    /*
      The same lab with VyOS in RouterOS's place, under UML. Its
      interfaces are eth1 and eth2, which VyOS requires; vivarium names
      them by MAC and adds their addresses.
    */
    vyos-bgp = mkTest {
      name = "vyos-bgp";
      nodes.r1 = {
        vivarium.interfaces = {
          eth1 = {
            segment = "core";
            addresses = [ "10.0.0.1/30" ];
          };
          eth2 = {
            segment = "west";
            addresses = [ "10.1.0.1/24" ];
          };
        };
        vivarium.memory = "1024M";
        imports = [ vivarium.kinds.vyos ];
        vivarium.vyos = images.vyos-rolling // {
          config = ''
            protocols {
                bgp {
                    address-family {
                        ipv4-unicast {
                            network 10.1.0.0/24 {
                            }
                        }
                    }
                    neighbor 10.0.0.2 {
                        address-family {
                            ipv4-unicast {
                            }
                        }
                        remote-as "65002"
                        timers {
                            holdtime "3"
                            keepalive "1"
                        }
                    }
                    parameters {
                        router-id "10.0.0.1"
                    }
                    system-as "65001"
                }
            }
            system {
                host-name "r1"
            }
          '';
        };
      };
      nodes.f1 = frrPeer;
      nodes.a = lanHost "west" "10.1.0.2" "10.1.0.1" "10.2.0.0";
      nodes.b = lanHost "east" "10.2.0.2" "10.2.0.1" "10.1.0.0";
      phases.bgp = {
        script = ./tests/phases/vyos-bgp.py;
        after = [ "boot" ];
      };
    };

    /*
      Can an unprivileged, read-only reader open the guest's Nix
      database, as on a running host? harmonia's cache is one.
    */
    # `vivarium.recipes.cache`: a guest fetches what only the cache guest
    # holds, and a path nobody holds fails at once.
    cache = mkTest {
      name = "cache";
      vivarium.recipes.cache = {
        enable = true;
        roots = [ "${pkgs.hello}" ];
      };
      nodes.client = { };
      # In no guest's `lateClosure`: `client` must fetch it from the cache.
      settings.hello = "${pkgs.hello}";
      phases.cache = {
        script = ./tests/phases/cache.py;
        after = [ "boot" ];
      };
    };

    nix-db = mkTest {
      name = "nix-db";
      nodes.one =
        { pkgs, ... }:
        {
          environment.systemPackages = [ pkgs.sqlite ];
        };
      phases.nix-db = {
        script = ./tests/phases/nix-db.py;
        after = [ "boot" ];
      };
    };

    /*
      openSUSE with a writable /nix (`vivarium.image.writableStore`):
      Nix there opens a local store whose database knows the guest's
      closure, and a collection keeps the agent.
    */
    suse-nix = mkTest {
      name = "suse-nix";
      settings.nix = "${pkgs.nix}";
      settings.hello = "${pkgs.hello}";
      nodes.suse =
        { settings, ... }:
        {
          imports = [ vivarium.kinds.image ];
          # Not in the image's own roots: the runner registers it after boot.
          vivarium.lateClosure = [ settings.hello ];
          vivarium.image = images.opensuse-leap-16_0 // {
            writableStore = true;
          };
          vivarium.memory = "1024M";
        };
      phases.nix = {
        script = ./tests/phases/suse-nix.py;
        after = [ "boot" ];
      };
    };

    /*
      Ansible from a NixOS controller configures openSUSE over a segment.

      `ctl` holds the key; the SUSE seed authorises it for `ansible`, with
      sudo. The playbook writes what Ansible's facts say the target is,
      and the agent reads it back on SUSE. An unreachable address is the
      negative control.
    */
    suse-ansible =
      let
        key = pkgs.runCommand "ansible-key" { nativeBuildInputs = [ pkgs.openssh ]; } ''
          mkdir $out
          ssh-keygen -q -t ed25519 -N "" -C ansible -f $out/id_ed25519
        '';
        playbook = pkgs.writeText "site.yml" (
          builtins.toJSON [
            {
              hosts = "suse";
              become = true;
              tasks = [
                {
                  name = "say what the target is";
                  "ansible.builtin.copy" = {
                    dest = "/etc/vivarium-ansible";
                    content = "{{ ansible_distribution }} {{ ansible_distribution_version }}\n";
                    mode = "0644";
                  };
                }
                {
                  name = "a user";
                  "ansible.builtin.user".name = "deploy";
                }
              ];
            }
          ]
        );
      in
      mkTest {
        name = "suse-ansible";
        settings = {
          playbook = "${playbook}";
          key = "${key}/id_ed25519";
        };
        nodes.suse = {
          imports = [ vivarium.kinds.image ];
          vivarium.image = images.opensuse-leap-16_0 // {
            userData.users = [
              "default"
              {
                name = "ansible";
                sudo = "ALL=(ALL) NOPASSWD:ALL";
                ssh_authorized_keys = [ (builtins.readFile "${key}/id_ed25519.pub") ];
              }
            ];
          };
          vivarium.memory = "1024M";
          vivarium.interfaces.lab = {
            segment = "lab";
            addresses = [ "10.9.0.2/24" ];
          };
        };
        nodes.ctl =
          { settings, ... }:
          {
            vivarium.lateClosure = [
              settings.playbook
              settings.key
            ];
            vivarium.interfaces.lab = {
              segment = "lab";
              addresses = [ "10.9.0.1/24" ];
            };
            environment.systemPackages = [
              pkgs.ansible
              pkgs.openssh
            ];
          };
        phases.ansible = {
          script = ./tests/phases/suse-ansible.py;
          after = [ "boot" ];
        };
      };

    /*
      Can a test read a guest's screen, type on it and point at it?

      `desk` has a screen with a getty on tty1; `blind` has none, the
      negative control. CI runs `screen.qemu`, where `blind` is QEMU too.
    */
    screen = mkTest {
      name = "screen";
      nodes.desk = {
        vivarium.backend = "qemu";
        vivarium.display.enable = true;
        services.getty.autologinUser = "root";
      };
      nodes.blind = { };
      phases.screen = {
        script = ./tests/phases/screen.py;
        after = [ "boot" ];
      };
    };

    /*
      Do phases on disjoint guests run at once, and is everything they
      say still filed under the right phase?

      `left` holds `a`, `right` holds `b`, and `both` holds every guest
      and comes after them. Three claims, each read from events.jsonl:

      - `left` and `right` overlap in time.
      - `both` overlaps nothing: a phase without `nodes` runs alone. This
        is the negative control, and what a scheduler that ignored
        `nodes` would fail.
      - Every journal marker and every print carries the phase that held
        its guest, although two phases were writing at once.
    */
    parallel =
      let
        run = mkTest {
          name = "parallel";
          nodes.a = { };
          nodes.b = { };
          phases = {
            left = {
              script = ./tests/phases/parallel.py;
              nodes = [ "a" ];
              after = [ "boot" ];
            };
            right = {
              script = ./tests/phases/parallel.py;
              nodes = [ "b" ];
              after = [ "boot" ];
            };
            both = {
              script = ./tests/phases/parallel.py;
              after = [
                "left"
                "right"
              ];
            };
          };
        };
      in
      pkgs.runCommand "vivarium-check-parallel" { passthru.session = run; } ''
        ${pkgs.python3.interpreter} ${./tests/checks}/parallel.py ${run.attempt}
        touch $out
      '';

    /*
      Does every guest import `defaults`, and know each peer by name?

      No guest names another in its own configuration: /etc/hosts comes
      from the `nodes` every guest receives, as in nixos-test.
    */
    peers = mkTest {
      name = "peers";
      defaults.environment.etc."vivarium-defaults".text = "from-defaults\n";
      nodes.server.vivarium.lan = {
        network = "peers";
        address = "192.168.99.2/24";
      };
      nodes.client.vivarium.lan = {
        network = "peers";
        address = "192.168.99.3/24";
      };
      phases.peers = {
        script = ./tests/phases/peers.py;
        after = [ "boot" ];
      };
    };

    /*
      Can a phase switch a running guest to another configuration and
      back, as `nixos-rebuild` would, without losing the agent?
    */
    switch = mkTest {
      name = "switch";
      nodes.one =
        { pkgs, ... }:
        {
          environment.etc."vivarium-config".text = "one\n";
          vivarium.configurations.two = {
            environment.etc."vivarium-config".text = lib.mkForce "two\n";
            systemd.services.only-in-two = {
              wantedBy = [ "multi-user.target" ];
              serviceConfig = {
                Type = "oneshot";
                RemainAfterExit = true;
                ExecStart = "${pkgs.coreutils}/bin/true";
              };
            };
            # A changed agent unit: without `restartIfChanged = false` the
            # switch restarts the agent under the command that runs it.
            systemd.services.vivarium-agent.environment.VIVARIUM_SWITCHED = "1";
          };
        };
      phases.switch = {
        script = ./tests/phases/switch.py;
        after = [ "boot" ];
      };
    };

    /*
      Does each guest see only its own closure, on every backend, and can
      it still add paths? `k` is the one with no bind-mount view: libkrun
      shares the host store with the closure as an allowlist.
    */
    store-view = mkTest {
      name = "store-view";
      nodes.u.vivarium.backend = "uml";
      nodes.q.vivarium.backend = "qemu";
      nodes.c.vivarium.backend = "container";
      nodes.k.vivarium.backend = "krun";
      phases.view = {
        script = ./tests/phases/store-view.py;
        after = [ "boot" ];
      };
    };

    /*
      nixpkgs' own nixos tests, through the mapper, unchanged. Cheap ones
      with no screen: `nixos-tests.simple-vm`, and so on.
    */
    nixos-tests = lib.genAttrs [
      "simple-vm"
      "systemd-no-tainted"
      "oh-my-zsh"
    ] (name: fromNixosTest (pkgs.path + "/nixos/tests/${name}.nix"));

    /*
      nixpkgs' tests that read a screen, unchanged: `cage` is Wayland and
      sets `enableOCR`, `xterm` is X11 and is found by its script. By
      hand: they need /dev/kvm.
    */
    nixos-screen-tests = lib.genAttrs [
      "cage"
      "xterm"
    ] (name: fromNixosTest (pkgs.path + "/nixos/tests/${name}.nix"));

    /*
      What the mapper carries beyond memory and cores, each a way a
      nixpkgs test passed under nixos-test and failed here.

      - `additionalPaths` makes `hello` valid in the guest: without it the
        same command exits 1, because the store holds the path and the
        database does not.
      - A path only the testScript names is in the guest.
      - `succeed` returns stdout alone; stderr goes to the console.
    */
    nixos-compat = fromNixosTest (
      let
        named = pkgs.writeText "named-by-the-script" "reached";
      in
      {
        name = "nixos-compat";
        nodes.machine = {
          virtualisation.diskSize = 1536;
          virtualisation.additionalPaths = [ pkgs.hello ];
        };
        testScript = ''
          machine.succeed("nix-store --check-validity ${pkgs.hello}")
          size = int(machine.succeed("df --block-size=1M --output=size / | tail -1"))
          assert size > 1024, f"the root image is {size} MiB, not the 1536 asked for"

          got = machine.succeed("cat ${named}")
          assert got == "reached", f"a path the script names reads {got!r}"

          out = machine.succeed("echo to-stdout; echo to-stderr >&2")
          assert out.strip() == "to-stdout", f"succeed returned {out!r}"
        '';
      }
    );

    # Does `.driverInteractive` behave like nixos-test's? tests/interactive.py.
    interactive =
      let
        run = mkTest {
          name = "interactive";
          nodes.one = { };
          interactive.nodes.one.environment.etc."vivarium-interactive".text = "yes\n";
          phases.hello = {
            script = ./tests/phases/hello.py;
            after = [ "boot" ];
          };
        };
      in
      pkgs.runCommand "vivarium-check-interactive" { passthru.session = run; } ''
        export HOME=$TMPDIR
        ${pkgs.python3.interpreter} ${./tests/interactive.py} ${lib.getExe run.driverInteractive} "$TMPDIR"
        touch $out
      '';

    # Does a run leave nothing behind, however it ends? tests/cleanup.py.
    # `.qemu` and `.krun` ask for KVM, as every test's variants do.
    cleanup =
      let
        run = mkTest {
          name = "cleanup";
          nodes.one = { };
          phases.hold = {
            script = ./tests/phases/hold.py;
            after = [ "boot" ];
          };
        };
        on =
          backend:
          let
            session = if backend == "uml" then run else run.${backend};
          in
          pkgs.runCommand "vivarium-check-cleanup-${backend}"
            {
              passthru.session = session;
              inherit (session.attempt) requiredSystemFeatures;
            }
            ''
              ${pkgs.python3.interpreter} ${./tests/cleanup.py} ${lib.getExe cli} ${session.spec} "$TMPDIR" ${backend}
              touch $out
            '';
      in
      on "uml"
      // {
        qemu = on "qemu";
        krun = on "krun";
      };

    # Can `exec` reach a run that is not paused? tests/live-exec.py.
    live-exec =
      let
        run = mkTest {
          name = "live-exec";
          nodes.one = { };
          phases.live = {
            script = ./tests/phases/live.py;
            after = [ "boot" ];
          };
        };
      in
      pkgs.runCommand "vivarium-check-live-exec" { passthru.session = run; } ''
        ${pkgs.python3.interpreter} ${./tests/live-exec.py} ${lib.getExe cli} ${run.spec} "$TMPDIR"
        touch $out
      '';

    /*
      The command lines a run prints -- monitor, exec, continue -- run as
      they are, copied from its output: an agent that knows nothing of
      vivarium can reach a run it only started. tests/copy-commands.py.
    */
    copy-commands =
      let
        run = mkTest {
          name = "copy-commands";
          nodes.one = { };
          phases.later = {
            script = ./tests/phases/independent.py;
            after = [ "boot" ];
          };
        };
      in
      pkgs.runCommand "vivarium-check-copy-commands" { passthru.session = run; } ''
        ${pkgs.python3.interpreter} ${./tests/copy-commands.py} ${lib.getExe cli} ${run.spec} "$TMPDIR"
        touch $out
      '';

    /*
      One guest of each backend on one segment, reaching each other over
      IP by name. `mixed` and `container-lan` each prove one pair; this
      holds all three at once.
    */
    backends = mkTest {
      name = "backends";
      nodes =
        lib.mapAttrs
          (name: backend: {
            vivarium = {
              inherit backend;
              lan = {
                network = "backends";
                address =
                  {
                    u = "10.57.0.1/24";
                    q = "10.57.0.2/24";
                    c = "10.57.0.3/24";
                  }
                  .${name};
              };
            };
          })
          {
            u = "uml";
            q = "qemu";
            c = "container";
          };
      phases.reach = {
        script = ./tests/phases/backends.py;
        after = [ "boot" ];
      };
    };

    /*
      Can a suite that has to run inside a guest report like one that
      runs on the host?

      pynixd's suites start daemons and build into stores they make, so
      they run in the guest and write JUnit there. The session reads
      `/artifacts/junit/*.xml` back at the end of each phase and each
      test becomes a case: an event with its machine and phase, and a
      case in the run's own junit.xml.

      Also the two things generating phases from one list needs: one
      script serving two phases through `vms.phase`, and a value crossing
      from one phase to the next through `vms.shared`.
    */
    guest-suites =
      let
        run = mkTest {
          name = "guest-suites";
          nodes.one = { };
          phases = {
            census = {
              script = ./tests/phases/suite.py;
              after = [ "boot" ];
            };
            suite = {
              script = ./tests/phases/suite.py;
              after = [ "census" ];
            };
          };
        };
      in
      pkgs.runCommand "vivarium-check-guest-suites" { passthru.session = run; } ''
        ${pkgs.python3.interpreter} ${./tests/checks}/guest_suites.py ${run.attempt}
        touch $out
      '';

    /*
      Does the `suites` recipe run pytest inside a guest?

      The suite holds one passing and one failing test. The check's own
      run fails, and both become cases with the guest's name. A driver
      run given `-- -k test_ok` passes, so the driver's arguments reach
      the pytest in the guest.
    */
    suite-recipe =
      let
        run = mkTest {
          name = "suite-recipe";
          nodes.one = { };
          vivarium.recipes.suites.inner = {
            node = "one";
            tests = ".";
            directory = "${./tests/guest-suite}";
            pytest = "${pkgs.python3.withPackages (p: [ p.pytest ])}/bin/pytest";
            timeout = 600;
          };
        };
      in
      pkgs.runCommand "vivarium-check-suite-recipe" { passthru.session = run; } ''
        ${pkgs.python3.interpreter} ${./tests/checks}/suite_recipe.py ${run.attempt} ${lib.getExe run.driver} "$TMPDIR"
        touch $out
      '';

    /*
      Does `--kernel` boot the kernel it names?

      The by-hand loop for kernel work: `make` in a tree, then boot what it
      made, without a Nix build per change. A copy of the Nix-built kernel
      outside the store stands in for a working tree's `linux`. Both
      directions: the copy boots and the run passes, and a path that is not
      a kernel fails the boot -- so what boots is the override, not the
      spec's kernel behind it.
    */
    kernel-override =
      let
        run = mkTest {
          name = "kernel";
          nodes.one = { };
        };
      in
      pkgs.runCommand "vivarium-check-kernel-override" { passthru.session = run; } ''
        ${pkgs.python3.interpreter} ${./tests/checks}/kernel_override.py ${run.spec} ${lib.getExe run.driver} "$TMPDIR"
        touch $out
      '';

    /*
      Can a person reach into a paused run?

      The loop this is for: changing a phase costs an evaluation and a
      store path, and changing a guest costs an image, so the fast way is
      neither -- pause with the guests up and send Python in. And can
      `ctl stop` end the run? tests/checks/breakpoint.py.
    */
    breakpoint =
      let
        run = mkTest {
          name = "breakpoint";
          nodes.one = { };
          phases.later = {
            script = ./tests/phases/independent.py;
            after = [ "boot" ];
          };
        };
      in
      pkgs.runCommand "vivarium-check-breakpoint" { passthru.session = run; } ''
        ${pkgs.python3.interpreter} ${./tests/checks}/breakpoint.py ${lib.getExe cli} ${lib.getExe run.driver} "$TMPDIR"
        touch $out
      '';

    /*
      Can a guest host a userspace filesystem?

      The question a build sandbox cannot answer for itself: its /dev has
      null, zero, random and little else, so a FUSE mount is out of reach
      there however the test is written.  A guest brings its own kernel
      and so its own /dev/fuse.

      Unprivileged mounting takes programs.fuse, which is opt in on NixOS
      and is what puts a setuid fusermount3 under /run/wrappers.  The
      wrappers themselves a guest already has.
    */
    fuse = mkTest {
      name = "fuse";
      phases.test = {
        script = ./tests/fuse.py;
        after = [ "boot" ];
      };
      nodes.node = {
        programs.fuse.enable = true;
        programs.fuse.userAllowOther = true;
        environment.systemPackages = [
          pkgs.bindfs
          pkgs.util-linux
        ];
        users.users.alice = {
          isNormalUser = true;
          uid = 1000;
        };
      };
    };

    /*
      Does a guest give its memory back?

      Both backends, and the same script: UML reports free pages through
      `madvise(MADV_REMOVE)` and QEMU through virtio-balloon, and a test
      sees one number either way. Issues #12 and #4.
    */
    memory = mkTest {
      name = "memory";
      phases.test = {
        script = ./tests/memory.py;
        after = [ "boot" ];
      };
      nodes.node = {
        # Large enough that reading the guest's own closure is page cache
        # and not pressure, which is what lets the test attribute what it
        # frees afterwards.
        vivarium.memory = "1024M";
      };
    };

    /*
      Does a guest give its cold page cache back with nobody asking?
      DAMON_RECLAIM reclaims it and free page reporting returns it.
      krun, the backend it is on by default for; `.qemu` asks the same.
    */
    reclaim = mkTest {
      name = "reclaim";
      backend = "krun";
      phases.test = {
        script = ./tests/reclaim.py;
        after = [ "boot" ];
      };
      nodes.node = {
        vivarium.memory = "1024M";
        vivarium.reclaim = {
          enable = true;
          minAge = 10;
        };
      };
    };

    # How much does a segment between two guests actually carry?
    iperf = mkTest {
      name = "iperf";
      phases.test = {
        script = ./tests/iperf.py;
        after = [ "boot" ];
      };
      nodes = iperfNodes;
    };

    /*
      What the uplink carries, passt against gvproxy (nixidae/nixidae#1163).

      Two krun guests in one run, so the two are measured back to back on
      the same host. The host runs the iperf3 client through each guest's
      forward, both directions.
    */
    uplink-iperf = mkTest {
      name = "uplink-iperf";
      backend = "krun";
      settings.iperf3 = lib.getExe pkgs.iperf3;
      phases.test = {
        script = ./tests/uplink-iperf.py;
        after = [ "boot" ];
      };
      nodes = lib.genAttrs [ "passt" "gvproxy" ] (proxy: {
        imports = [ ./modules/iperf3.nix ];
        services.iperf3-server.enable = true;
        vivarium.uplink.proxy = proxy;
        vivarium.forward = [ { ports = [ 5201 ]; } ];
      });
    };

    /*
      Does a container run at all?

      One guest, and the narrow question the cluster test answers
      only after an hour: whether the kernel has what runc wants,
      whether the images imported, and whether a container of
      symlinks can reach the store they point into.
    */
    containerd = mkTest {
      name = "containerd";
      vivarium.recipes.kubernetes.enable = true;
      phases.test = {
        script = ./tests/containerd.py;
        after = [ "boot" ];
      };
      nodes.node =
        { config, ... }:
        {
          services.vivarium-k8s = {
            enable = true;
            role = "worker";
            runtimes = [
              "crun"
              "youki"
            ]
            ++ lib.optional (config.vivarium.backend != "uml") "runsc";
          };
          vivarium = {
            memory = "1024M";
            diskSize = 2048;
            lan = {
              network = "containerd";
              address = "10.101.0.1/24";
            };
          };
        };
      settings = {
        inherit (k8sImages) sandboxImage entrypoints;
        kubernetesVersion = pkgs.kubernetes.version;
      };
    };

    /*
      The same questions under Kata Containers, a QEMU VM per pod, on
      either CRI. `kata` is CRI-O, as OpenShift runs it.

      By hand, not in CI, like `nested`: a GitHub runner's KVM does not
      nest again.
    */
    kata = kataTest "crio";
    kata-containerd = kataTest "containerd";

    /*
      containerd and a user-namespaced container from an image with zero
      layers: tests/phases/zero-layers.py. `stock` is nixpkgs' containerd
      and must fail those cases, which Lillecarl/containerd#1 describes;
      `fixed` is the same build with that fix and must run all of them.
      `unit` runs containerd's overlay snapshotter test as root, where it
      does not skip, and checks that it tells the fix from the unfixed
      code.

      When `stock` stops failing, containerd in nixpkgs carries a fix:
      drop the patch.
    */
    containerd-zero-layers =
      let
        fix = ./pkgs/containerd-userns-zero-layers.patch;
        fixed = pkgs.containerd.overrideAttrs (old: {
          patches = (old.patches or [ ]) ++ [ fix ];
          # So `containerd --version` shows which guest carries the fix.
          makeFlags = map (f: if lib.hasPrefix "VERSION=" f then "${f}+zero-layers-fix" else f) old.makeFlags;
        });
        node =
          containerd: address:
          { ... }:
          {
            # The module names pkgs.containerd.
            nixpkgs.overlays = lib.optional (containerd != null) (_: _: { inherit containerd; });
            services.vivarium-k8s = {
              enable = true;
              role = "worker";
              runtimes = [ "crun" ];
            };
            vivarium = {
              memory = "1024M";
              diskSize = 2048;
              lan = {
                network = "zero-layers";
                inherit address;
              };
            };
          };
        image =
          name: layers:
          pkgs.runCommand "zero-layers-${name}.tar" { nativeBuildInputs = [ pkgs.python3 ]; } ''
            python3 ${./tests/oci-image.py} $out zero-layers.test/${name}:1 ${lib.escapeShellArgs layers}
          '';
        /*
          `go test -c` of the overlay snapshotter. `control` is the unfixed
          code with the fixed test file, so a test that cannot tell the two
          apart fails the check.
        */
        overlayTests =
          name: patchCode:
          pkgs.runCommandCC "containerd-overlay-tests-${name}"
            {
              nativeBuildInputs = [
                pkgs.go
                pkgs.patch
              ];
            }
            ''
              cp -r ${pkgs.containerd.src} src && chmod -R u+w src && cd src
              cp plugins/snapshots/overlay/overlay.go overlay.go.orig
              patch -p1 < ${fix}
              ${lib.optionalString (!patchCode) "cp overlay.go.orig plugins/snapshots/overlay/overlay.go"}
              export HOME=$TMPDIR GOCACHE=$TMPDIR/cache GOFLAGS=-mod=vendor
              go test -c -o $out ./plugins/snapshots/overlay
            '';
      in
      mkTest {
        name = "containerd-zero-layers";
        vivarium.recipes.kubernetes.enable = true;
        defaults =
          { settings, ... }:
          {
            vivarium.lateClosure = [
              settings.busybox
              settings.fixedTests
              settings.controlTests
            ]
            ++ lib.attrValues settings.images;
          };
        nodes = {
          stock = node null "10.106.0.1/24";
          fixed = node fixed "10.106.0.2/24";
        };
        settings = {
          fixed = [ "fixed" ];
          busybox = "${pkgs.pkgsStatic.busybox}/bin/busybox";
          images = {
            zero = "${image "zero" [ ]}";
            # One layer and still no /proc: the failure is the layer count.
            one-no-proc = "${image "one-no-proc" [ "etc/,etc/marker" ]}";
            one-proc = "${image "one-proc" [ "proc/,dev/,sys/" ]}";
          };
          fixedTests = "${overlayTests "fixed" true}";
          controlTests = "${overlayTests "control" false}";
        };
        phases = {
          stock = {
            script = ./tests/phases/zero-layers.py;
            after = [ "boot" ];
            nodes = [ "stock" ];
          };
          fixed = {
            script = ./tests/phases/zero-layers.py;
            after = [ "boot" ];
            nodes = [ "fixed" ];
          };
          unit = {
            script = ./tests/phases/overlay-unit.py;
            after = [ "stock" ];
            nodes = [ "stock" ];
          };
        };
      };

    # The same questions under CRI-O. nixidae/nixidae#534.
    crio = mkTest {
      name = "crio";
      vivarium.recipes.kubernetes.enable = true;
      phases.test = {
        script = ./tests/containerd.py;
        after = [ "boot" ];
      };
      nodes.node =
        { config, ... }:
        {
          services.vivarium-k8s = {
            enable = true;
            role = "worker";
            cri = "crio";
            # Not runsc, which runs no container under CRI-O; see the
            # module's assertion.
            runtimes = [
              "crun"
              "youki"
            ];
          };
          vivarium = {
            memory = "1024M";
            diskSize = 2048;
            lan = {
              network = "crio";
              address = "10.105.0.1/24";
            };
          };
        };
      settings = {
        inherit (k8sImages) sandboxImage entrypoints;
        kubernetesVersion = pkgs.kubernetes.version;
      };
    };

    # Does a real workload come up across three nodes?  Far heavier
    # than the others: three guests, a control plane and a container
    # runtime, so this one wants a builder rather than a laptop.
    k8s = mkTest {
      name = "k8s";
      phases.test = {
        script = ./tests/k8s.py;
        after = [ "boot" ];
      };
      nodes = k8sNodes;
      vivarium.recipes.kubernetes.enable = true;
      settings = {
        inherit (k8sConfig.services.vivarium-k8s) podSubnet workloadImage;
        kubernetesVersion = pkgs.kubernetes.version;
      };
    };

    # Are the images we build the ones kubeadm will go looking for?
    # Cheap, and the alternative is finding out from an
    # ImagePullBackOff twenty minutes into the cluster test.
    check-k8s-images = k8sImages.check;

    # And does kubeadm accept the configuration we generate for it?
    check-k8s-config =
      pkgs.runCommand "kubeadm-config-valid" { nativeBuildInputs = [ pkgs.kubernetes ]; }
        (
          lib.concatMapStrings (file: ''
            echo "validating ${file}"
            kubeadm config validate --config ${file}
          '') (lib.attrValues k8sConfig.system.build.kubeadmConfigs)
          + "touch $out"
        );

    # The scripts in tests/, against the library they drive. Also the
    # check that `typeCheck` itself works.
    check-scripts = vivarium.typeCheck {
      name = "own-scripts";
      # `.py` only: a run outside the sandbox leaves `__pycache__` beside
      # them, and zuban has nothing to say about a `.pyc`.
      scripts = lib.filter (p: lib.hasSuffix ".py" (toString p)) (
        # `recipes` as well as `tests`: a recipe is shipped for other
        # projects to enable, so one that does not type check breaks a
        # consumer rather than this repository.
        lib.filesystem.listFilesRecursive ./recipes ++ lib.filesystem.listFilesRecursive ./tests
      );
      # What `python-packages` gives its phase, and what the cluster tests
      # import.
      extraPackages = [
        pkgs.python3Packages.humanize
        pkgs.python3Packages.tomli-w
        k8s
      ];
      # The check scripts import their shared `harness` by name.
      extraPaths = [ ./tests/checks ];
    };

    # zuban, strict, over `runner` and `session` and their unit tests. Each
    # package's `mypy.ini` sets its own floor.
    check-python =
      let
        python = nixvenv {
          python = pkgs.python3;
          packages = ps: [
            runner
            session
            k8s
            ps.pytest
          ];
        };
      in
      pkgs.runCommand "vivarium-check-python" { nativeBuildInputs = [ pkgs.zuban ]; } ''
        for package in ${./pkgs/vivarium-runner} ${./pkgs/vivarium} ${./pkgs/vivarium-k8s}; do
          (cd $package && zuban mypy --python-executable ${python}/lib/venv/bin/python3)
        done
        touch $out
      '';
  };

  /*
    Name a run and it is evaluated, built and run, with no `nix build`
    first:

        nix run --file . packages.vivarium -- run checks.pytest-phase --out ./out -- -k hostname

    The `vivarium` CLI. Built on nanopynix and huggorm, so not a check
    and not a dependency of `nix build` or `nix run` of a test.
    `vivarium-eval.tests` holds its unit tests.
  */
  vivarium-eval = import ./pkgs/vivarium-eval { inherit pkgs sources; };

  /*
    What a person or an agent installs: `vivarium` (the CLI, built on
    huggorm), `vivarium-mcp`, and the agent skill at
    `share/skills/vivarium/vivarium`. Not the runner: a run names its own,
    and every command it prints is absolute. Not inside `cli`: every
    test's spec names `cli`, so a skill inside it would rebuild every test
    on each edit.
  */
  tools = pkgs.runCommand "vivarium-tools" { meta.mainProgram = "vivarium"; } ''
    mkdir -p $out/bin $out/share/skills/vivarium
    ln -s ${vivarium-eval}/bin/vivarium ${vivarium-eval}/bin/vivarium-mcp $out/bin/
    cp -r ${./skill/vivarium} $out/share/skills/vivarium/vivarium
  '';
in
# The schema of nixidae/nixidae#1103. Every other name is under `internal`.
{
  # The library, for a caller that writes its own test.
  lib = {
    inherit
      mkNode
      mkTest
      fromNixosTest
      runner
      session
      k8s
      cli
      typeCheck
      images
      k8sImages
      ;
    inherit (vivarium) cacheFacts kinds pathsIn;
    # The pyproject project of `runner`, for a caller that resolves it in a
    # Python set of its own.
    runnerSource = ./pkgs/vivarium-runner;
  };

  /*
    What CI builds. Every test also answers to `.uml` and `.qemu`.

        nix build --file . checks.lan          # as CI runs it
        nix build --file . checks.lan.qemu     # the same test, as machines
        nix build --file . checks.iperf.qemu   # what the segment carries there

    Nothing is duplicated to make that work: one script, one set of node
    configurations, and neither knows which machine it got.

    A check holds the default of each, which is UML. A `.qemu` variant
    asks the daemon for the `kvm` feature, and a builder without
    `/dev/kvm` does not fail it -- it refuses to build it at all, which
    would stop CI rather than report anything. That is the only reason
    they are not checks, and it goes away once we know what our runners
    have.
  */
  checks = tests;

  # The workflows that run the tests. nixidae renders each one to
  # `.github/workflows/vivarium-<name>.yml`.
  ciTestWorkflows = ci.workflows;

  packages = {
    inherit
      cli
      runner
      session
      k8s
      vivarium-eval
      ;
    vivarium = tools;
    libkrun = pkgs.callPackage ./pkgs/libkrun/package.nix { };
    # The slowest thing in the repository and the same for every guest, so
    # CI builds it once on its own and lets the cache hand it to the test
    # jobs.
    umlKernel = k8sConfig.system.build.umlKernel;
    default = cli;
  };
  # A kubeadm node, for a guest of `lib.mkTest`, and the guest kinds.
  nixosModules = {
    k8s = ./modules/k8s.nix;
  }
  // vivarium.kinds;
  easykubenixModules = { };
  docs.default = docs;

  internal = {
    inherit
      demo
      store
      k8s-pull
      uplink
      incr
      ;
    inherit (demo.config.system.build) vivariumRunner vivariumRootImage toplevel;

    /*
      The same kernel, compiled through ccache into `/ccache`, for a patch
      series rebuilt as CI builds it but without starting cold each time.
      Only a builder that mounts the directory can build it:

        pynix build --file . --attr internal.umlKernelCcache --namespaced \
          --sandbox-path /ccache=$HOME/.cache/uml-ccache

      Measured: 122s cold, 31s after a one-line change. `--kernel` with
      `make ARCH=um` in a tree is still the inner loop.
    */
    umlKernelCcache = k8sConfig.system.build.umlKernel.override { ccacheDir = "/ccache"; };

    # A guest to poke at by hand, running one program.
    speedtest = pkgs.writeShellScriptBin "vivarium-speedtest" ''
      exec ${demo.config.system.build.vivariumRunner}/bin/vivarium-run --command speedtest-cli "$@"
    '';

    /*
      `vivarium-mcp` driven the way Claude Code drives it: JSON-RPC over stdio,
      against a run that fails a phase on purpose. The channel events --
      paused, failed, finished -- are the claim; `exec` and `events` are
      checked against the paused guests.

      By spec, so nothing here evaluates. Not a check, for the same reason
      `vivarium-eval` is not: CI would have to build nanopynix.
    */
    mcp-check = pkgs.runCommand "vivarium-check-mcp" { nativeBuildInputs = [ pkgs.python3 ]; } ''
      export HOME="$TMPDIR"
      python3 ${./tests/mcp_driver.py} ${vivarium-eval}/bin/vivarium-mcp ${tests.pytest-phase.session.spec}
      touch $out
    '';
  };
}

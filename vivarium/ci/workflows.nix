# The workflows, as data. nixidae turns each attribute here into
# .github/workflows/vivarium-<name>.yml, and its check fails when the two
# have drifted.
{ lib, ghalib }:
let
  ci = import ./lib.nix { inherit lib; };
  inherit (ci)
    steps
    job
    selectable
    bootstrap
    ;

  # A job that boots a guest needs the user namespace, whether the guest is
  # a UML process inside the Nix sandbox or a QEMU machine outside it: the
  # sandbox unshares one, and so does the runner. A QEMU guest needs /dev/kvm on
  # top, and `uid-range`: virtiofsd serves a guest user's file as that
  # user, which a sandbox mapping one uid cannot (see `featuresFor`).
  #
  # `mkMerge` and not `//`, which is shallow: an addition under `nix` would
  # otherwise replace the whole install block and take the cache with it.
  sandboxBootstrap = lib.mkMerge [
    bootstrap
    { userNamespaces.enable = true; }
  ];

  guestBootstrap = lib.mkMerge [
    sandboxBootstrap
    {
      openKvm.enable = true;
      nix.install.uidRange = true;
    }
  ];

  # A container guest needs `uid-range` and /dev/net, but no /dev/kvm.
  containerBootstrap = lib.mkMerge [
    sandboxBootstrap
    {
      nix.install.uidRange = true;
      nix.install.devNet = true;
    }
  ];

  # A run that holds a guest of each backend needs all of it.
  everyBackendBootstrap = lib.mkMerge [
    guestBootstrap
    { nix.install.devNet = true; }
  ];

  /*
    The integration tests, and what each one costs.

    A guest is a process, so these are bounded by the runner's four
    cores rather than by anything virtual: `k8s` boots three guests and
    waits on a control plane, which is why it gets its own job and a
    much larger cap instead of being folded in with the others.
  */
  tests = {
    lan = {
      description = "two guests on a segment see each other";
      timeoutMinutes = 45;
      # The one job left on UML, and the reason the kernel is still built.
      # Everything else moved to QEMU for the speed, and a UML regression
      # has to fail here rather than somewhere else.
      backend = "uml";
    };
    segment-mtu = {
      description = "a segment's MTU and hub reach each interface on it";
      timeoutMinutes = 30;
      backend = "uml";
    };
    segment-cut = {
      description = "the runner's hub cuts, restores and captures a segment";
      timeoutMinutes = 30;
      backend = "uml";
    };
    memory = {
      # One guest, one closure read, and four numbers. Its own job
      # because it is the only check that measures the host rather than
      # the guest, so a regression in it says something no other job can.
      description = "a guest gives its memory back";
      timeoutMinutes = 20;
      # UML, although the test passes on both: this is the job that would
      # catch a regression in the kernel patch, and only a UML guest has
      # that patch in it.
      backend = "uml";
    };
    frr-clos = {
      # Seven UML guests, 40 seconds once built: the kernel's VXLAN, VRF
      # and multipath options only matter here.
      description = "BGP unnumbered and EVPN on a CLOS of FRR switches";
      timeoutMinutes = 30;
      backend = "uml";
    };
    ipsec = {
      # Seven UML guests, about three minutes once built: the kernel's
      # XFRM, ESP and xfrmi options only matter here.
      description = "strongSwan site-to-site with certificates, BGP over the tunnels";
      timeoutMinutes = 45;
      backend = "uml";
    };
    suse-ansible = {
      # openSUSE Leap's own cloud image on the UML kernel, configured by
      # Ansible from a NixOS guest: the kernel's XFS, btrfs and VFAT only
      # matter here.
      description = "Ansible configures openSUSE Leap over a segment";
      timeoutMinutes = 30;
      backend = "uml";
    };
    vyos-uml = {
      # VyOS's own squashfs on the UML kernel: the kernel's squashfs,
      # initrd and the netfilter options VyOS's boot asks for only
      # matter here. 42 s once built.
      description = "VyOS boots from its ISO's squashfs and applies config.boot";
      timeoutMinutes = 30;
      backend = "uml";
    };
    routeros = {
      # MikroTik CHR under QEMU, driven through its serial console: the
      # only test of ConsoleControl against a real guest. 20 s once built.
      description = "RouterOS boots, applies its config disk and answers on its console";
      timeoutMinutes = 20;
    };
    routeros-router = {
      # CHR behind a QEMU router guest: ether1's default DHCP client is
      # what makes RouterOS a guest the automatic router serves.
      description = "RouterOS leases its uplink from the router and reaches the host";
      timeoutMinutes = 20;
    };
    routeros-bgp = {
      # eBGP between CHR and FRR, and two LANs that only BGP joins. It
      # covers routeros-route, which stays a local test.
      description = "RouterOS and FRR exchange routes over eBGP";
      timeoutMinutes = 20;
    };
    vyos-bgp = {
      # The VyOS analogue under UML, and the only test of the ethtool
      # kernel fix: VyOS runs ethtool on a down interface at boot.
      description = "VyOS and FRR exchange routes over eBGP";
      timeoutMinutes = 30;
      backend = "uml";
    };
    sonic = {
      # SONiC VS under QEMU, driven through its serial console: the
      # shell-dialect console guest, where RouterOS is the CLI-dialect
      # one. Boots in ~21 s; the cap is the 1.9 GB image fetch on a cold
      # cache.
      description = "SONiC VS boots and answers a shell on its serial console";
      timeoutMinutes = 30;
    };
    sonic-router = {
      # SONiC behind a QEMU router guest: eth0's DHCP client is what
      # makes SONiC a guest the automatic router serves. SONiC (4 GiB)
      # plus the router; the cap is the SONiC image fetch.
      description = "SONiC leases its uplink from the router and reaches the host";
      timeoutMinutes = 30;
    };
    oob = {
      # A management segment the test serves: a NixOS services guest runs
      # Kea DHCP4 and a peer leases a management address. Two small NixOS
      # guests, 2.6 s once built.
      description = "a services guest hands out a management lease with Kea";
      timeoutMinutes = 20;
    };
    oob-sonic = {
      # The fabric end to end: SONiC's eth0 leases from the run's router,
      # the management switch, and the services guest provisions it over
      # SSH -- a `config` command per setting, then a whole config_db.json
      # pushed and applied with `config reload`. SONiC (4 GiB), a NixOS
      # guest and the router; the cap is the SONiC image fetch. The reload phase measures ~140 s
      # (swss-ready wait plus the service restart) and retries the reload
      # for up to 240 s, so size any timeout change above that, not just
      # the boot.
      description = "the services guest provisions SONiC over SSH, by command and by config_db.json";
      timeoutMinutes = 30;
    };
    oob-routeros = {
      # Functional management VRF: RouterOS's ether1 (the uplink) leases
      # from the run's router in a mgmt VRF, isolated from the dataplane.
      # CHR under QEMU plus two NixOS guests and the router.
      description = "RouterOS keeps its management in a VRF, isolated from the dataplane";
      timeoutMinutes = 20;
    };
    oob-gateway = {
      # The services guest as the OOB gateway: the host reaches a guest
      # whose uplink is on the management segment through one forward
      # that DNATs to it. Two small NixOS guests, 1.6 s once built.
      description = "the services guest is the single forwarding point to an out-of-band guest";
      timeoutMinutes = 20;
    };
    oob-uml = {
      # The oob lease over UML processes: proves a UML guest's uplink
      # joins a management segment (vec0 on a segment fd, not vsock),
      # which VyOS needs. Needs the UML kernel job.
      description = "a UML guest's uplink joins a management segment";
      timeoutMinutes = 20;
      backend = "uml";
    };
    vyos-oob = {
      # VyOS on a management segment the test serves: eth0 (the uplink)
      # leases its management address from the services guest's Kea.
      # VyOS under UML plus a NixOS guest; the cap is the VyOS ISO fetch
      # on a cold cache.
      description = "VyOS leases its management address from a segment the test serves";
      timeoutMinutes = 30;
      backend = "uml";
    };
    vyos-router = {
      # VyOS behind the run's router: eth0's DHCP client is what makes
      # VyOS a guest the automatic router serves.
      description = "VyOS leases its uplink from the router and reaches the host";
      timeoutMinutes = 30;
      backend = "uml";
    };
    vyos-oob-vrf = {
      # Functional management VRF, the VyOS analogue of oob-routeros: eth0
      # leases from the run's router in a mgmt VRF, isolated from the
      # dataplane. VyOS under UML plus two NixOS guests and the router.
      description = "VyOS keeps its management in a VRF, isolated from the dataplane";
      timeoutMinutes = 30;
      backend = "uml";
    };
    router = {
      # The run's router guest on UML: its egress on the host serves the
      # guest's vsock as a vhost-user device (nixidae/nixidae#1404).
      description = "guests behind a router guest reach the host and are reached";
      timeoutMinutes = 30;
      backend = "uml";
    };
    router-network = {
      # Two segments with a router, each in a VRF of its own on it.
      description = "router networks reach the host and not each other";
      timeoutMinutes = 30;
      backend = "uml";
    };
    forward-router = {
      description = "the forward contract holds behind the router";
      timeoutMinutes = 30;
      backend = "uml";
    };
    router-qemu = {
      # `.qemu` puts the router on QEMU, with a vhost-user-vsock-pci device
      # on the egress's socket.
      test = "router";
      backend = "qemu";
      description = "guests behind a QEMU router guest";
      timeoutMinutes = 30;
    };
    router-krun = {
      # `.krun` puts the router on libkrun too, with libkrun's own vsock.
      test = "router";
      backend = "krun";
      description = "guests behind a libkrun router guest";
      timeoutMinutes = 30;
    };
    # A guest that is its own router: relay and ruleset in a namespace,
    # its egress on the host. One job per vsock transport.
    lan-vsock = {
      description = "two guests on vsock uplinks hold lan's contract";
      timeoutMinutes = 30;
      backend = "uml";
    };
    forward-vsock = {
      description = "the forward contract holds on a QEMU guest's vsock uplink";
      timeoutMinutes = 30;
    };
    lan-vsock-krun = {
      test = "lan-vsock";
      backend = "krun";
      description = "two libkrun guests on vsock uplinks hold lan's contract";
      timeoutMinutes = 30;
    };
    lan-vsock-container = {
      # No vsock device: the relay and the egress share unix sockets.
      test = "lan-vsock";
      backend = "container";
      description = "two container guests on vsock uplinks hold lan's contract";
      timeoutMinutes = 30;
    };
    pytest-coverage = {
      # 100% of `sample` only if a guest's coverage data reached the host.
      attr = "checks.pytest-coverage";
      description = "coverage from tests in a guest reaches the host's pytest-cov";
      timeoutMinutes = 30;
    };
    # What a krun guest's vsock uplink carries, both ways; the numbers are in the log.
    uplink-iperf = {
      backend = "krun";
      description = "what vsock carries for a libkrun guest";
      timeoutMinutes = 30;
    };
    uplink-iperf-router = {
      backend = "krun";
      description = "what a libkrun guest behind a libkrun router carries";
      timeoutMinutes = 30;
    };
    pytest-host = {
      # Plain pytest drives QEMU guests, each run from a prebuilt `.up`
      # (`lib.pytestCheck`, nixidae/nixidae#1413).
      attr = "checks.pytest-host";
      description = "a pytest suite drives guests through the vivarium plugin";
      timeoutMinutes = 30;
    };
    nix-db = {
      description = "an unprivileged read-only reader opens the guest's Nix database";
      timeoutMinutes = 20;
    };
    suse-nix = {
      # The same image with a writable /nix: the kernel's overlayfs over
      # hostfs, and a collection that keeps only what is rooted.
      description = "Nix on openSUSE Leap, with a writable store";
      timeoutMinutes = 30;
      backend = "uml";
    };
    screen = {
      description = "the host reads a guest's screen, types on it and points at it";
      timeoutMinutes = 30;
    };
    iperf = {
      description = "what a segment between two guests carries";
      timeoutMinutes = 45;
    };
    forward = {
      description = "the host reaches a service inside a guest";
      timeoutMinutes = 45;
    };
    fuse = {
      # One guest, no network, and a closure of bindfs and util-linux on
      # top of the base system.  The kernel comes from the cache, so what
      # is left is a root image and a six-second boot: about a minute in
      # all, so the cap is ten times what the job costs rather than fifty.
      description = "a userspace filesystem, as root and as a user";
      timeoutMinutes = 15;
    };
    artifacts = {
      description = "what a guest writes reaches the host, per guest";
      timeoutMinutes = 15;
    };
    reboot = {
      description = "a guest reboots into the system its bootloader names";
      timeoutMinutes = 15;
    };
    boot-counting = {
      description = "systemd-boot falls back from an entry that never completes a boot";
      timeoutMinutes = 20;
    };
    tpm-unlock = {
      description = "the initrd unlocks LUKS from a TPM on Secure Boot firmware, and asks once it cannot";
      timeoutMinutes = 20;
    };
    containerd = {
      description = "a container runs, out of the host's store";
      timeoutMinutes = 45;
    };
    crio = {
      description = "containerd's questions, under CRI-O";
      timeoutMinutes = 45;
      backend = "uml";
    };
    containerd-zero-layers = {
      # Two containerd builds, nixpkgs' and the patched one.
      description = "a user-namespaced container from an image with zero layers, with and without the fix";
      timeoutMinutes = 60;
      backend = "uml";
    };
    k8s = {
      description = "a three-node kubeadm cluster";
      timeoutMinutes = 120;
      # Everything the cluster is built on, asked in a couple of minutes
      # rather than found out from a control plane that never becomes
      # healthy.  Nothing here is worth two hours if a container cannot
      # start at all.
      after = [ "test-containerd" ];
    };
    k8s-host = {
      description = "the host's kubectl reaches a one-node cluster";
      timeoutMinutes = 45;
      after = [ "test-containerd" ];
    };

    # krun is for dense NixOS guests: its virtio-fs DAX window maps the
    # host's page cache of the store, where QEMU copies it into each guest
    # (3-node k8s on dynhetz: peak host PSS 2250 against 3347 MiB). These
    # are the krun-specific paths: the reboot through libkrun's exit code,
    # and the cluster krun is meant to carry. Not `memory`: it measures the
    # balloon, and a krun guest has none.
    reboot-krun = {
      test = "reboot";
      backend = "krun";
      description = "a libkrun guest reboots into the system its bootloader names";
      # The step gets 10 minutes less. Warm, the test takes 30 s; with
      # libkrun or the guests changed, copying the closure alone ran past
      # a 5-minute step after the test had passed.
      timeoutMinutes = 25;
    };
    # A krun guest has no balloon to inflate, so DAMON_RECLAIM is how it
    # gives memory back unasked.
    reclaim-krun = {
      test = "reclaim";
      backend = "krun";
      description = "a libkrun guest gives cold page cache back by itself";
      timeoutMinutes = 15;
    };
    containerd-krun = {
      test = "containerd";
      backend = "krun";
      description = "a container runs in a libkrun guest, out of the host's store";
      timeoutMinutes = 45;
    };
    # docs/design/dinix-guests.md. A krun guest only, so far.
    dinix = {
      backend = "krun";
      description = "a dinix guest boots its own kernel under libkrun, with dinit as PID 1";
      timeoutMinutes = 20;
      after = [ "direct-kernel" ];
    };
    k8s-krun = {
      test = "k8s";
      backend = "krun";
      description = "a three-node kubeadm cluster on libkrun guests";
      timeoutMinutes = 120;
      after = [ "test-containerd-krun" ];
    };
  };

  /*
    What each step outside a test job builds. The steps read it, and so
    do `attrs` and `internal.ci`.
  */
  built = {
    kernel = [ "packages.umlKernel" ];
    direct-kernel = [ "packages.directKernel" ];
    container-probe = [ "checks.container-probe-tun" ];
    test-container = [
      "checks.container"
      "checks.container-lan"
    ];
    checks = [
      "checks.ruff"
      "checks.check-scripts"
      "checks.docs"
      "checks.check-python"
      "checks.check-k8s-images"
      "checks.check-k8s-config"
      "checks.ci-set"
      "packages.agent.tests"
    ];
    sessions = [
      "checks.phase-rules"
      "checks.knobs"
      "checks.only-rules"
      "checks.on-demand"
      "checks.recipes"
      "checks.suite-recipe"
      "checks.stream"
      "checks.pytest-phase"
      "checks.breakpoint"
      "checks.live-exec"
      "checks.copy-commands"
      "checks.guest-suites"
      "checks.kernel-override"
      "checks.parallel"
      "checks.report"
      "checks.peers"
      "checks.switch"
      "checks.cleanup"
      "checks.interactive"
      "checks.verdict"
    ];
    features = [
      "checks.cloud"
      "checks.launch"
      "checks.python-packages"
      "checks.cache"
      "checks.lan-router"
      "checks.nixos-compat"
      "checks.nixos-tests.simple-vm"
      "checks.nixos-tests.oh-my-zsh"
      "checks.nixos-tests.systemd-no-tainted"
      "checks.suse"
      "checks.suse-router"
    ];
    backends = [
      "checks.store-view"
      "checks.segments"
      "checks.backends"
      "checks.mixed"
    ];
    eval = [
      "packages.vivarium-eval.tests"
      "internal.mcp-check"
    ];
  };

  /*
    What a job runs as a driver and not as a build, by job id: each
    reaches the network, which a sandbox has not. Not in `internal.ci`.
  */
  driven = {
    test-k8s-pull = "internal.k8s-pull";
    test-uplink-offline = "internal.uplink";
  };

  # Building the kernel once and letting the cache hand it to the test
  # jobs saves repeating a ten-minute build three times over.
  kernelJob = job {
    id = "kernel";
    timeoutMinutes = 60;
    cond = selectable "kernel";
    steps = [
      (steps.build {
        name = "Build the UML kernel";
        attrs = built.kernel;
        timeoutMinutes = 45;
      })
    ];
  };

  # A dinix guest's kernel, as `kernelJob` does the UML one. nixpkgs' whole
  # config, so it gets its own job and a larger cap; the cache hands it on.
  directKernelJob = job {
    id = "direct-kernel";
    timeoutMinutes = 180;
    cond = selectable "direct-kernel";
    steps = [
      (steps.build {
        name = "Build the dinix guest kernel";
        attrs = built.direct-kernel;
        timeoutMinutes = 170;
      })
    ];
  };

  /*
    One test, on one backend.

    QEMU by default, because a guest runs on the processor instead of
    trapping every syscall into the host kernel, and the job then waits on
    no `umlKernel` -- the ten-minute build the `kernel` job does once.

    It costs /dev/kvm, which `guestBootstrap` provides: the runner has the
    device, and the udev rule lets the sandbox's build user open it.  A
    `.qemu` derivation asks the daemon for the `kvm` feature, so a runner
    without it refuses the build -- a job that does not start, not a red
    one.
  */
  testSpec =
    name:
    {
      description,
      timeoutMinutes,
      after ? [ ],
      backend ? "qemu",
      # The check this job runs, when the job's name is not the check's.
      test ? name,
      # The whole attribute, for a check with no backend variants.
      attr ? if backend == "uml" then "checks.${test}" else "checks.${test}.${backend}",
    }:
    {
      inherit
        description
        timeoutMinutes
        after
        backend
        test
        attr
        ;
    };

  testJob =
    name: spec:
    let
      inherit (testSpec name spec)
        description
        timeoutMinutes
        after
        backend
        test
        attr
        ;
    in
    job {
      id = "test-${name}";
      needs = (lib.optional (backend == "uml") "kernel") ++ after;
      inherit timeoutMinutes;
      cond = selectable "test-${name}";
      # krun asks what QEMU asks: /dev/kvm, and `uid-range` for virtio-fs.
      ghanix =
        {
          uml = sandboxBootstrap;
          container = containerBootstrap;
        }
        .${backend} or guestBootstrap;
      steps = [
        (steps.build {
          name = "Run ${test} on ${backend}: ${description}";
          attrs = [ attr ];
          timeoutMinutes = timeoutMinutes - 10;
        })
        # The step log holds the last 50 lines of the run. The attempt
        # holds the rest: each console, events.jsonl, the artifacts.
        {
          name = "Copy the run's evidence";
          "if" = "failure()";
          run = "cp -r --dereference \"$(nix eval --raw --file . ${attr}.attempt.outPath)\" attempt && chmod -R u+w attempt";
        }
        {
          name = "Keep the evidence of a red run";
          "if" = "failure()";
          uses = "actions/upload-artifact@v4";
          "with" = {
            name = "attempt-${name}";
            path = "attempt";
            retention-days = 7;
            if-no-files-found = "warn";
          };
        }
      ];
    };

  /*
    The tests whose QEMU job above holds the backend contract -- a test
    never knows which backend it got -- with the other backends each runs
    on. Not `fuse` on a container guest: `modules/container.nix` refuses
    FUSE there.
  */
  contract = {
    forward = [
      "uml"
      "krun"
      "container"
    ];
    artifacts = [
      "uml"
      "krun"
      "container"
    ];
    fuse = [
      "uml"
      "krun"
    ];
  };

  allTests =
    tests
    // lib.concatMapAttrs (
      test: backends:
      lib.genAttrs' backends (
        backend:
        lib.nameValuePair "${test}-${backend}" (
          tests.${test}
          // {
            inherit test backend;
          }
        )
      )
    ) contract;

  testJobs = lib.mapAttrs' (
    name: spec: lib.nameValuePair "test-${name}" (testJob name spec)
  ) allTests;

  /*
    A guest as a container, in the Nix sandbox.

    The derivation asks for `uid-range`: a build as root with 65536 ids
    and a cgroup of its own, which systemd as PID 1 in the container
    needs. The runner's daemon offers it only when told to, which is
    ghanix's `uidRange`; `devNet` adds /dev/net, for the uplink and the
    LAN. The probe goes first on its own, so a runner that lacks any of
    it fails in seconds, by name. `container-lan` holds a UML guest,
    hence the kernel.
  */
  containerJob = job {
    id = "test-container";
    needs = [ "kernel" ];
    timeoutMinutes = 30;
    cond = selectable "test-container";
    ghanix = containerBootstrap;
    steps = [
      (steps.build {
        name = "Probe: can this sandbox run a container guest with a LAN";
        attrs = built.container-probe;
        timeoutMinutes = 5;
      })
      (steps.build {
        name = "Run container and container-lan: NixOS guests under crun, in the sandbox";
        attrs = built.test-container;
        timeoutMinutes = 20;
      })
    ];
  };

  /*
    The one test that reaches a registry: a QEMU node that pulls its own
    images. The sandbox has no network, so this is `nix run`, not
    `nix build`.

    The runner needs an unprivileged user namespace for a guest's store
    view, and Ubuntu denies one by default: `ghanix.userNamespaces`
    allows it. A developer machine allows the namespace, so only a
    runner shows its absence, and this job is where it shows.
  */
  pullJob = job {
    id = "test-k8s-pull";
    timeoutMinutes = 30;
    cond = selectable "test-k8s-pull";
    ghanix = guestBootstrap;
    steps = [
      {
        name = "Run k8s-pull: a node that pulls its images, on a virtual machine";
        timeout-minutes = 20;
        run = "nix run --print-build-logs --file . ${driven.test-k8s-pull}.driver -- --out ./out";
      }
    ];
  };

  /*
    `--offline` on a connected runner: the guest reaches nothing past the
    host. A build cannot ask this, because its sandbox has no network
    with or without the flag. UML, so the kernel.
  */
  offlineJob = job {
    id = "test-uplink-offline";
    needs = [ "kernel" ];
    timeoutMinutes = 30;
    cond = selectable "test-uplink-offline";
    ghanix = sandboxBootstrap;
    steps = [
      {
        name = "Run uplink offline: nothing leaves the host";
        timeout-minutes = 20;
        run = "nix run --print-build-logs --file . ${driven.test-uplink-offline}.driver -- --out ./out --offline";
      }
    ];
  };

  /*
    The checks that need no guest.

    First, and on their own: each is a small derivation, so a stale
    render or an image tag kubeadm no longer asks for is reported in a
    minute rather than after the cluster test has spent an hour finding
    out the hard way.
  */
  checksJob = job {
    id = "checks";
    timeoutMinutes = 30;
    cond = selectable "checks";
    steps = [
      (steps.build {
        name = "Check the generated files, ruff, the scripts, the Python types, the docs, the images and the kubeadm config";
        attrs = built.checks;
        timeoutMinutes = 20;
      })
    ];
  };
  /*
    The session's own rules, on the smallest guests there are.

    One job rather than one per check: each boots a single guest with
    nothing on it, so the whole set costs less than any one of the tests
    above. What they answer is about the runner and not about
    the backend -- what a failure skips, what a knob is worth, what
    `--only` leaves alone -- so one backend is enough.

    UML, because `mkTest` defaults to it. That is why this waits on
    `kernel` although nothing in it is a kernel test.
  */
  sessionJob = job {
    id = "sessions";
    needs = [ "kernel" ];
    timeoutMinutes = 30;
    cond = selectable "sessions";
    ghanix = sandboxBootstrap;
    steps = [
      (steps.build {
        name = "The session: phase rules, knobs, --only, on-demand boot, recipes, guest suites, stream, pytest, breakpoints, printed commands, parallel, report, peers, switch, cleanup, interactive, red verdicts";
        attrs = built.sessions;
        timeoutMinutes = 20;
      })
    ];
  };
  /*
    What a test asks of vivarium beyond the session: the cloud API,
    templates, Python packages, the cache recipe, the router, the
    nixos-test mapper with nixpkgs' own tests, and a foreign image. One
    UML guest or two each; its own job, so the session job's step keeps
    its margin when the guests are built cold.
  */
  featuresJob = job {
    id = "features";
    needs = [ "kernel" ];
    timeoutMinutes = 45;
    cond = selectable "features";
    ghanix = sandboxBootstrap;
    steps = [
      (steps.build {
        name = "Features: cloud, launch, Python packages, cache, router, the nixos-test mapper, openSUSE";
        attrs = built.features;
        timeoutMinutes = 35;
      })
    ];
  };

  /*
    Runs that hold guests of different backends at once: each store view,
    segments across backends, a ping between each pair. A UML guest waits
    on the kernel; a container guest needs /dev/net.
  */
  backendsJob = job {
    id = "backends";
    needs = [ "kernel" ];
    timeoutMinutes = 30;
    cond = selectable "backends";
    ghanix = everyBackendBootstrap;
    steps = [
      (steps.build {
        name = "Backends together: store views, segments, all four on one segment, UML beside QEMU";
        attrs = built.backends;
        timeoutMinutes = 20;
      })
    ];
  };

  /*
    The `vivarium` CLI and its MCP server, both built on huggorm.

    Its own job: they build nanopynix, which no other job needs. The MCP
    check drives a run that fails a phase on purpose, so it boots a UML guest.
  */
  evalJob = job {
    id = "eval";
    needs = [ "kernel" ];
    timeoutMinutes = 60;
    cond = selectable "eval";
    ghanix = sandboxBootstrap;
    steps = [
      (steps.build {
        name = "The vivarium CLI's tests, and the MCP server driven as Claude Code drives it";
        attrs = built.eval;
        timeoutMinutes = 50;
      })
    ];
  };
in
{
  /**
    Every attribute a CI job builds, as a path from the top of
    `default.nix`: `internal.ci` builds them all.
  */
  attrs =
    lib.concatLists (lib.attrValues built)
    ++ lib.mapAttrsToList (name: spec: (testSpec name spec).attr) allTests;

  /**
    What a CI job runs as a driver, as a path from the top of `default.nix`.
  */
  driven = lib.attrValues driven;

  /**
    The checks no CI job runs, each with the reason. `checks.ci-set`
    fails on a check that is neither here nor in a job, and on a name
    here that is both or names no check.

    No job runs on macOS either: forward.py's darwin branches have only
    unit tests, which patch `sys.platform`.
  */
  byHand = {
    nested = "a GitHub runner is itself a VM, and its KVM does not nest a second time";
    kata = "a Kata pod is a VM in the guest, and a GitHub runner's KVM does not nest";
    kata-containerd = "a Kata pod is a VM in the guest, and a GitHub runner's KVM does not nest";
    nixos-screen-tests = "nixpkgs' own tests, unchanged; `screen` runs vivarium's screen methods in CI";
    appliance = "`routeros` runs RouterOS CHR in CI";
    vyos = "`vyos-uml`, `vyos-bgp` and `vyos-oob` run VyOS in CI";
    routeros-route = "`routeros-bgp` runs the same routing through RouterOS in CI";
    container-probe = "`container-probe-tun` asks the same and more in the container job";
  };

  # Through ghanix's schema rather than straight to YAML. That is what
  # turns each job's `ghanix` attribute into steps, drops the options a job
  # left unset, and refuses a job with no steps at all.
  workflows.ci = ghalib.evalWorkflow {
    name = "CI";
    on = {
      push = { };
      pull_request = { };
      workflow_dispatch.inputs.jobs = {
        description = "Comma-separated job IDs to run; empty runs all of them";
        required = false;
        type = "string";
        default = "";
      };
    };
    jobs = {
      checks = checksJob;
      sessions = sessionJob;
      features = featuresJob;
      backends = backendsJob;
      eval = evalJob;
      kernel = kernelJob;
      direct-kernel = directKernelJob;
      test-k8s-pull = pullJob;
      test-uplink-offline = offlineJob;
      test-container = containerJob;
    }
    // testJobs;
  };
}

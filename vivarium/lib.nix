# mkNode and mkTest, as a library.
#
# A file that takes a package set, so a caller with its own guests and its own
# script reaches them: easykubenix drives `ekn kubeapply` against a cluster
# this builds.
#
#     let vivarium = (import sources.vivarium { inherit pkgs sources; }).lib;
#     in vivarium.mkTest { name = "..."; nodes = { ... }; phases.check.script = ./mine.py; }
#
# Nothing here is specific to the tests in this repository. `flake.nix` and
# `default.nix` both call it, so the two doors cannot drift apart.
{
  # A caller passes its own, so nixvenv comes from the same nixidae as it.
  sources ? import ./nix/sources.nix,
  # The host's: the runner and every program it starts.
  pkgs,
  lib ? pkgs.lib,
  # The guests'. A guest is Linux, so a macOS host builds it for Linux on
  # the same processor (nixidae/nixidae#1163).
  guestPkgs ?
    if pkgs.stdenv.hostPlatform.isDarwin then
      import pkgs.path { system = "${pkgs.stdenv.hostPlatform.parsed.cpu.name}-linux"; }
    else
      pkgs,
}:
let
  inherit ((import sources.nixvenv { inherit pkgs; }).lib) nixvenv;
  # A check's builder: fails on its attempt's `status`, or links each
  # `name=what` entry of the attempt into `$out`.
  readVerdict =
    attempt: entries:
    "${pkgs.python3.interpreter} ${./nix/verdict.py} ${attempt} $out ${lib.escapeShellArgs entries}";
in
rec {
  inherit guestPkgs;

  /**
    The library a test script imports, reachable without evaluating a
    guest: a caller puts it in the Python it type checks with.  It carries
    `py.typed`, so a type checker reads `vms.node` as a `Machine`.

        nixvenv { python = pkgs.python3; packages = _: [ vivarium.runner ]; }
  */
  runner = pkgs.callPackage ./pkgs/vivarium-runner { vivarium-agent = agent; };

  /**
    The guest's agent and the control channel's protocol, `vivarium_agent`.
    Every guest runs it; `runner` speaks to it. A guest holds this and not
    the runner, so a runner edit rebuilds no guest.
  */
  agent = pkgs.callPackage ./pkgs/vivarium-agent {
    dinit-client = pkgs.callPackage "${sources.dinix}/dinit-client/package.nix" { };
  };

  /**
    The session, and the `vivarium` CLI that drives one.

    The session owns the sequence; `runner` above is the mechanism it
    drives. See `docs/design/runner.md`.
  */
  session = pkgs.callPackage ./pkgs/vivarium {
    vivarium-runner = runner;
    vivarium-agent = agent;
    vivarium-ca = ca;
    vivarium-socks = socks;
  };

  /**
    `vivarium_socks`: the router's SOCKS5 proxy into the lab, and the
    host's split proxy in front of it that a session serves.
  */
  socks = pkgs.callPackage ./pkgs/vivarium-socks { };

  /**
    `vivarium_ca`: the name-constrained lab CA a session makes for a run
    that asks, `vms.trust`.
  */
  ca = pkgs.callPackage ./pkgs/vivarium-ca { };

  /**
    `vivarium_k8s`, the cluster library. `vivarium.recipes.kubernetes` puts
    it in the run's venv.
  */
  k8s = pkgs.callPackage ./pkgs/vivarium-k8s { vivarium-runner = runner; };

  /**
    The router's LoadBalancer controller, `vivarium-lb`, a guest program.
    `vivarium.recipes.kubernetes.loadBalancer` runs it.
  */
  /**
    cert-manager's binaries, images and manifest, for
    `vivarium.recipes.kubernetes.certManager`.
  */
  certManager = guestPkgs.callPackage ./pkgs/cert-manager { };

  lb = guestPkgs.callPackage ./pkgs/vivarium-lb {
    kr8s = guestPkgs.python3Packages.callPackage "${sources.nanopynix}/nix/kr8s.nix" {
      src = sources.kr8s;
    };
  };

  /**
    The runner, `vivarium-runner`, in a venv that holds `session`. What a
    test's spec names and its driver runs; it never evaluates. The CLI a
    person runs is `vivarium` from `packages.vivarium`, built on huggorm. A child of
    `sys.executable` then imports what the program imports, with no
    PYTHONPATH: the runner starts its launcher and its probes that way.
  */
  cli = nixvenv {
    python = pkgs.python3;
    packages = _: [ session ];
    executables = [ "vivarium-runner" ];
  };

  /**
    Other distributions' cloud images, for `vivarium.image`:

        nodes.suse = {
          imports = [ vivarium.kinds.image ];
          vivarium.image = vivarium.images.opensuse-leap-16_0;
        };
  */
  images = pkgs.callPackage ./images.nix { };

  /**
    The guest kinds: modules that replace a guest's NixOS with another
    system and set how the host drives it. A guest imports one, then sets
    its option (`vivarium.image`, `vivarium.vyos`, ...).
  */
  kinds = {
    appliance = ./modules/appliance.nix;
    dinix = lib.modules.importApply ./modules/dinix-guest.nix { inherit sources; };
    image = ./modules/image-guest.nix;
    routeros = ./modules/routeros-guest.nix;
    sonic = ./modules/sonic-guest.nix;
    vyos = ./modules/vyos-guest.nix;
  };

  /**
    Every store path a value names, for `vivarium.lateClosure`: each
    string with context, and each derivation, in nested attribute sets
    and lists.
  */
  pathsIn =
    value:
    if lib.isDerivation value then
      [ "${value}" ]
    else if builtins.isAttrs value then
      lib.concatMap pathsIn (lib.attrValues value)
    else if builtins.isList value then
      lib.concatMap pathsIn value
    else if builtins.isString value && builtins.hasContext value then
      # closureInfo keeps only the leading store path of a root, so a command
      # that names two (`…/bin/env …/bin/cgi-fcgi`) lost the second. A file
      # holding the string references every path it names.
      [ (if lib.isStorePath value then value else "${pkgs.writeText "vivarium-setting" value}") ]
    else
      [ ];

  /**
    A guest module that puts every store path in the test's `settings` in
    `vivarium.lateClosure`, so the guest's Nix database holds them as valid
    paths. Not a default: a guest kind with no store refuses a late closure.

    # Example

    ```nix
    defaults = vivarium.settingsClosure;
    ```
  */
  settingsClosure =
    { settings, ... }:
    {
      vivarium.lateClosure = pathsIn settings;
    };

  /**
    zuban over a caller's test scripts, against this library.

        typeCheck { scripts = [ ./tests/vivarium/run.py ]; }

    `mkTest` calls this itself -- see the run's `typeCheck`.  Call it
    directly for scripts that are not a test's.
  */
  typeCheck =
    {
      name ? "vivarium-test-scripts",
      scripts,
      extraPackages ? [ ],
      strict ? true,
      ignore ? [ ],
      # Directories of helper modules the scripts import; `mkTest`'s
      # `pythonPath`.
      extraPaths ? [ ],
    }:
    let
      # `session` brings pytest with it, so a pytest phase's tests and
      # conftest check against the same pytest that runs them.
      python = nixvenv {
        python = pkgs.python3;
        packages =
          _:
          [
            runner
            session
          ]
          ++ extraPackages;
      };

      config = pkgs.writeText "mypy.ini" (
        lib.generators.toINI { } {
          mypy = {
            python_version = lib.versions.majorMinor python.python.version;
            # Interpolated, so each directory is copied to the store.
            mypy_path = lib.concatMapStringsSep ", " (path: "${path}") extraPaths;
            # mypy's error codes, the way `writePython3Bin`'s `flakeIgnore`
            # is flake8's codes.
            disable_error_code = lib.concatStringsSep ", " ignore;
          }
          // (
            if strict then
              { strict = true; }
            else
              # An unannotated function is not checked at all, so a typo
              # in it costs a boot to find.
              {
                disallow_untyped_defs = true;
                check_untyped_defs = true;
              }
          );
        }
      );
    in
    pkgs.runCommand "typecheck-${name}"
      {
        nativeBuildInputs = [
          pkgs.zuban
        ];
      }
      ''
        ${python}/bin/python ${./nix/typecheck.py} $out ${config} ${python}/lib/venv/bin/python3 ${
          lib.concatMapStringsSep " " (script: lib.escapeShellArg "${script}") scripts
        }
      '';

  /**
    How one guest becomes a line in a spec.

    One place, so a field added here reaches every spec.
  */
  machineSpec =
    machine:
    {
      name = machine.networking.hostName;
      backend = machine.vivarium.backend;
      inherit (machine.vivarium) kind;
      index = machine.vivarium.index;
      memory = machine.vivarium.memory;
      seccomp = machine.vivarium.seccomp;
      cpus = machine.vivarium.cpus;
      sshPort = machine.vivarium.sshPort;
      mtu = machine.vivarium.mtu;
      network = machine.vivarium.lan.network;
      address = machine.vivarium.lan.address;
      interfaces = machine.vivarium.nics;
      uplink = machine.vivarium.uplink.segment;
      uplinkProxy = machine.vivarium.uplink.proxy;
      uplinkNetwork = (import ./nix/uplink.nix).spec;
      router = machine.vivarium.router.enable;
      inbound = machine.vivarium.inbound;
      inherit (machine.vivarium) control boot;
      nested = machine.vivarium.nestedVirtualization;
      display =
        if machine.vivarium.display.enable then
          { inherit (machine.vivarium.display) width height; }
        else
          null;
    }
    // lib.optionalAttrs (machine.vivarium.uplink.proxy == "router") {
      uplinkAddress = machine.vivarium.router.address machine.vivarium.index;
    }
    // lib.optionalAttrs machine.vivarium.router.enable {
      inherit (machine.vivarium.router) networkPorts;
      # Its own address on the bridge: its egress binds the router's own
      # forwards (the SOCKS proxy) as it does a routed guest's.
      uplinkAddress = (import ./nix/uplink.nix).gateway;
    }
    // lib.optionalAttrs (machine.vivarium.router.enable && machine.vivarium.router.socks.enable) {
      socks = {
        inherit (machine.vivarium.router.socks) port networks;
        domains = lib.attrNames machine.vivarium.router.socks.domains;
      };
    }
    // lib.optionalAttrs (machine.vivarium.persist != [ ]) {
      keep = machine.vivarium.persistSize;
    }
    // lib.optionalAttrs (machine.vivarium.nixDatabase.loader != null) {
      nixStore = machine.vivarium.nixDatabase.loader;
    }
    // lib.optionalAttrs (machine.vivarium.launch != null) {
      inherit (machine.vivarium) launch;
    }
    // {
      image = "${machine.vivarium.rootImage}";
    }
    # A guest kind's system is never built: its disk is not this NixOS.
    // lib.optionalAttrs (machine.vivarium.kind == "nixos") {
      systems = {
        toplevel = "${machine.system.build.toplevel}";
        configurations = lib.mapAttrs (_: system: "${system}") machine.system.build.vivariumConfigurations;
      };
    };

  /*
    What the daemon must give a run's derivation: what each guest's backend
    module asked for in `vivarium.requiredSystemFeatures`. UML asks for
    nothing, which is the whole point of UML.
  */
  featuresFor =
    machines: lib.unique (lib.concatMap (machine: machine.vivarium.requiredSystemFeatures) machines);

  /**
    Whether this sandbox can run a container guest, answered in seconds.

    The runner's own host checks (`vivarium_runner.container.probe`), in a
    derivation that asks for what a container session asks for. A missing
    piece fails here, named with its fix, rather than as a guest that does
    not boot minutes into a run. Every session with a container guest
    depends on it; a CI job can build it first on its own.

    `tun`: a LAN is wanted, so a tap device must be possible, which in a
    sandbox needs /dev/net in `extra-sandbox-paths`.
  */
  containerProbe =
    {
      tun ? false,
    }:
    pkgs.runCommand "vivarium-container-probe${lib.optionalString tun "-tun"}"
      {
        nativeBuildInputs = [
          (nixvenv {
            python = runner.pythonModule;
            packages = _: [ runner ];
          })
        ];
        requiredSystemFeatures = [ "uid-range" ];
      }
      ''
        set -o pipefail
        python -m vivarium_runner.crun_launch probe ${lib.optionalString tun "--tun"} | tee $out
      '';

  # The probe a set of machines needs, or null when none is a container.
  probeFor =
    machines:
    let
      containers = lib.filter (machine: machine.vivarium.backend == "container") machines;
    in
    if containers == [ ] then
      null
    else
      containerProbe {
        tun = lib.any (machine: machine.vivarium.nics != [ ]) containers;
      };

  /*
    Every peer's segment addresses in each guest's /etc/hosts: each as
    `<host>.<segment>`, and the `vec1` address as the bare hostname too,
    as nixos-test writes it from its `nodes`.
  */
  peersModule =
    { lib, nodes, ... }:
    let
      bare = cidr: lib.head (lib.splitString "/" cidr);
    in
    {
      networking.hosts = lib.mkMerge (
        lib.concatLists (
          lib.mapAttrsToList (
            _: peer:
            let
              host = peer.networking.hostName;
              address = peer.vivarium.lan.address;
            in
            lib.optional (address != null) { ${bare address} = [ host ]; }
            ++ lib.concatMap (
              nic: map (cidr: { ${bare cidr} = [ "${host}.${nic.segment}" ]; }) nic.addresses
            ) peer.vivarium.nics
          ) nodes
        )
      );
    };

  /**
    A guest: an ordinary NixOS configuration plus ./modules.

        mkNode { vivarium.backend = "qemu"; }

    `eval-config.nix` and not `lib.nixosSystem`. That name only exists on the
    lib a flake gets from nixpkgs' own `flake.nix`; `pkgs.lib` is the library
    itself and has never carried it. This is what the flake wrapper calls, and
    `system = null` is how it says that `nixpkgs.pkgs` below decides the
    platform.
  */
  mkNode =
    module:
    import (guestPkgs.path + "/nixos/lib/eval-config.nix") {
      inherit lib;
      system = null;
      # What the host runs for a guest: its launcher and the egress.
      specialArgs.hostPkgs = pkgs;
      modules = [
        (lib.modules.importApply ./modules { inherit sources; })
        { nixpkgs.pkgs = guestPkgs; }
        module
      ];
    };

  /**
    nixos-test's shape, mapped onto `mkTest`; see nixos-test.nix.
  */
  fromNixosTest = import ./nixos-test.nix { inherit pkgs lib mkTest; };

  /**
    A pytest suite that drives guests through `vivarium.pytest_host`, as
    a sandboxed check (nixidae/nixidae#1413). `runs` maps each name the
    suite's `@pytest.mark.vivarium` uses to a `mkTest`. Their `.up` is
    built ahead and handed to the plugin in `VIVARIUM_RUNS`, so nothing
    evaluates in the sandbox. `src` is the directory pytest runs in.

        vivarium.lib.pytestCheck {
          name = "guests";
          src = ./tests;
          runs = import ./tests/machines.nix { };
        }

    As a `mkTest`: `.attempt` never fails and writes `status`, `log`,
    `junit.xml` and each run's `--out` under `runs/`; the check reads
    `status`.
  */
  pytestCheck =
    {
      name,
      src,
      runs,
      # More distributions in the Python pytest runs on.
      pythonPackages ? [ ],
      # More pytest arguments.
      args ? [ ],
    }:
    let
      python = nixvenv {
        python = pkgs.python3;
        packages = _: [ session ] ++ pythonPackages;
      };
      programs = pkgs.writeText "vivarium-runs-${name}.json" (
        builtins.toJSON (lib.mapAttrs (_: run: lib.getExe run.up) runs)
      );
      attempt =
        pkgs.runCommand "vivarium-pytest-${name}-attempt"
          {
            requiredSystemFeatures = lib.unique (
              lib.concatMap (run: run.attempt.requiredSystemFeatures or [ ]) (lib.attrValues runs)
            );
            passthru = { inherit programs; };
          }
          ''
            export HOME="$TMPDIR"
            mkdir -p "$out"
            cp -r ${src} "$TMPDIR/src"
            chmod -R u+w "$TMPDIR/src"
            cd "$TMPDIR/src"
            status=0
            # A test that runs in a guest runs on this Python, put in the guest's store view.
            export VIVARIUM_PYTHON_CLOSURE=${pkgs.closureInfo { rootPaths = [ python ]; }}
            VIVARIUM_RUNS=${programs} ${python}/bin/python -m pytest -p no:cacheprovider \
              --junitxml="$out/junit.xml" --vivarium-out="$TMPDIR/runs" ${lib.escapeShellArgs args} \
              > "$out/log" 2>&1 || status=$?
            echo "$status" > "$out/status"
            # The runs live outside $out: Nix refuses a socket in an output,
            # and a run that was killed leaves its own.
            if [ -d "$TMPDIR/runs" ]; then
              find "$TMPDIR/runs" -type s -delete
              cp -r "$TMPDIR/runs" "$out/runs" || true
            fi
          '';
    in
    pkgs.runCommand "vivarium-pytest-${name}" { passthru = { inherit attempt; }; } (
      readVerdict attempt [
        "log=pytest's output"
        "junit.xml=one case per test"
        "runs=each run's --out"
      ]
    );

  /**
    The address and key of `vivarium.recipes.cache`, for what a run renders
    outside its own evaluation.
  */
  cacheFacts = import ./modules/recipes/cache-facts.nix;

  /**
    A run: guests, and the phases that drive them.

        mkTest {
          name = "mine";
          nodes.one = { };
          phases.check.script = ./check.py;
        }

    It takes a module, so a recipe can contribute a phase, the guest
    configuration that phase needs and the knobs it reads in a single
    import -- and a consumer overrides any of it the way they override a
    NixOS option.

    One program, run several ways. The derivation itself is the
    sandboxed run, which CI builds, and these come with it:

        .driver             the same run by hand, `--out` where you want it
        .driverDebug        the same, paused on the first failure
        .driverInteractive  a REPL, paused before the first phase, with
                            `interactive` merged in
        .up                 an environment: the phases, then `ready` with
                            the guests up until `ctl stop`
        .phases             what would run, in order, without booting
        .nodes, .config     the evaluated guests and run
        .systems            each guest's NixOS evaluation, for `extendModules`
        .extend { modules; }  the run with more modules
        .uml, .qemu, .container  every guest on that backend

    A phase script exports one coroutine:

        async def test(vms: Machines) -> None: ...

    It is imported rather than executed, so it may import whatever it
    likes and zuban checks it.

    **`after` is a dependency, not a hint.** A phase whose `after` failed
    is skipped, because running it against a world that was never built
    gives a second failure that says nothing. An `after` naming a phase
    that does not exist is an evaluation error, since the alternative is
    a phase that quietly never gets skipped.
  */
  mkTest =
    module:
    let
      run = lib.evalModules {
        modules = [
          ./modules/run.nix
          # The standard library is always imported and nothing in it is
          # on by default except `boot`, which every run wants and any
          # run can turn off. A recipe a consumer has to import by path
          # is a recipe nobody finds.
          ./modules/recipes
          module
          # Each guest's NixOS evaluation, for `extendModules`: a system a
          # guest can switch to, the one it booted plus a module. Lazy, as
          # `nodes` is for guests.
          { _module.args.systems = evaluated; }
          # Each template's evaluated configuration, as `nodes` holds a guest's.
          { _module.args.templateNodes = templates; }
        ];
        specialArgs = {
          # A run's modules mostly write guests.
          pkgs = guestPkgs;
          hostPkgs = pkgs;
          # The Python packages vivarium builds, for a recipe's
          # `python.packages`.
          vivariumPackages = {
            inherit
              runner
              session
              k8s
              lb
              certManager
              ;
          };
        };
      };

      cfg = run.config;

      # Every segment some guest joins, by an interface or its uplink.
      joined = lib.concatMap (
        node:
        map (nic: nic.segment) node.vivarium.nics
        ++ lib.optional (node.vivarium.uplink.segment != null) node.vivarium.uplink.segment
      ) (lib.attrValues nodes ++ lib.attrValues templates);
      unjoined = lib.filter (segment: !lib.elem segment joined) (lib.attrNames cfg.segments);

      routerNetworks = lib.attrNames (lib.filterAttrs (_: segment: segment.router != null) cfg.segments);

      # A router network's subnet and the uplink's: no two may overlap.
      prefixes = import ./nix/prefixes.nix lib;
      uplinkSpec = import ./nix/uplink.nix;
      unparsed = lib.filter (
        name: prefixes.fromIPv4 cfg.segments.${name}.router.address == null
      ) routerNetworks;
      overlapping =
        family: uplinkPrefix: parse: address:
        map
          (pair: "${family} ${lib.concatMapStringsSep " and " (entry: "${entry.name} (${entry.text})") pair}")
          (
            prefixes.overlaps (
              map (entry: entry // { prefix = parse entry.text; }) (
                [
                  {
                    name = "the uplink";
                    text = uplinkPrefix;
                  }
                ]
                ++ lib.concatMap (
                  name:
                  let
                    text = address cfg.segments.${name}.router;
                  in
                  lib.optional (text != null) { inherit name text; }
                ) (lib.subtractLists unparsed routerNetworks)
              )
            )
          );
      overlaps =
        overlapping "IPv4" uplinkSpec.subnet prefixes.fromIPv4 (router: router.address)
        ++ overlapping "IPv6" "${uplinkSpec.prefix6}/64" prefixes.fromIPv6 (router: router.address6);

      # A NixOS guest's toplevel checks its own assertions and warnings.
      # A guest of another kind never builds one, so the run checks them.
      unbuilt = lib.filter (node: node.vivarium.kind != "nixos") (
        lib.attrValues nodes ++ lib.attrValues templates
      );
      guestAssertions = lib.concatMap (
        node:
        map (each: each // { message = "${node.networking.hostName}: ${each.message}"; }) node.assertions
      ) unbuilt;
      guestWarnings = lib.concatMap (
        node: map (warning: "${node.networking.hostName}: ${warning}") node.warnings
      ) unbuilt;

      failed = lib.filter (each: !each.assertion) (
        cfg.assertions
        ++ [
          {
            assertion = unjoined == [ ];
            message = "segments: no guest joins ${lib.concatStringsSep ", " unjoined}; an interface names its segment in vivarium.interfaces.<name>.segment.";
          }
          {
            assertion = routerEnabled || routerNetworks == [ ];
            message = "segments: ${lib.concatStringsSep ", " routerNetworks} names a router, and router.enable is false.";
          }
          {
            assertion = unparsed == [ ];
            message = "segments: the router.address of ${lib.concatStringsSep ", " unparsed} is no IPv4 address with a prefix length, such as 10.9.0.1/24.";
          }
          {
            assertion = overlaps == [ ];
            message = "segments: these router networks overlap: ${lib.concatStringsSep "; " overlaps}.";
          }
        ]
        ++ guestAssertions
      );
      checkedConfig =
        if failed == [ ] then
          lib.showWarnings guestWarnings cfg
        else
          throw (lib.concatMapStringsSep "\n" (each: each.message) failed);

      inherit (checkedConfig) name;

      settingsFile = pkgs.writeText "vivarium-${name}-settings.json" (
        builtins.toJSON checkedConfig.settings
      );

      routerName = cfg.router.name;

      /*
        Whether the run has a router guest. Read from the user's guests,
        and the router joins `evaluated` after `cfg.nodes`, so that a
        guest's name does not wait on the decision.
      */
      routerEnabled =
        if cfg.router.enable != null then
          cfg.router.enable
        # `router.backend`, not `backend`: a `.krun` variant forces the
        # guests' backend and the router's, and leaves `backend` alone.
        else
          (pkgs.stdenv.hostPlatform.isDarwin && cfg.router.backend != "uml")
          || routerNetworks != [ ]
          || lib.any (hostName: wantsRouter hostName cfg.nodes.${hostName}) (
            lib.filter (hostName: hostName != routerName) (lib.attrNames cfg.nodes)
          );

      # A second evaluation, of the guest as it is without a router. Not
      # the guest's own: there `uplink.proxy`, `uplink.segment` and the
      # index read this decision, and the check for undeclared definitions
      # reads networkd's gateway, which reads `uplink.proxy`. The check
      # stays off here, so the probe reads no more than the option needs.
      wantsRouter =
        hostName: module:
        (evaluateWith hostName module 0 [ { _module.check = false; } ]).config.vivarium.uplink.wantsRouter;

      # The router's index is its own, so adding it moves no guest's
      # index, MAC or ssh port. The highest: a MAC octet is one byte.
      guestNames = lib.filter (hostName: !(routerEnabled && hostName == routerName)) (
        lib.attrNames cfg.nodes
      );
      indexOf =
        hostName:
        if routerEnabled && hostName == routerName then
          254
        else
          lib.lists.findFirstIndex (name: name == hostName) null guestNames;

      # What `router.enable` does to a guest. A guest of the router's
      # name merges into the router.
      routerModule =
        hostName:
        { config, ... }:
        {
          config = lib.mkIf routerEnabled (
            if hostName == routerName then
              {
                vivarium.router.enable = true;
                # Above `.qemu`'s and `.container`'s mkForce on `defaults`: the
                # router takes `router.backend`, whatever its guests take.
                vivarium.backend = lib.mkOverride 40 cfg.router.backend;
              }
            else
              {
                # A template's instance is never behind it (router.nix says why),
                # and keeps the uplink a guest has without a router.
                vivarium.uplink.proxy = lib.mkIf (!config.vivarium.template) (lib.mkDefault "router");
                # Here and not in the guest module: the `wantsRouter` probe
                # evaluates a guest without a router, and reads a null
                # segment as "the router serves my uplink".
                vivarium.uplink.segment = lib.mkIf (config.vivarium.uplink.proxy == "router") (
                  lib.mkDefault "uplink-${hostName}"
                );
                # A guest behind the router has no lease and no forwards without it.
                vivarium.requires = lib.mkIf (config.vivarium.uplink.proxy == "router") [ routerName ];
              }
          );
        };

      evaluate =
        hostName: module: evaluateWith hostName module (indexOf hostName) [ (routerModule hostName) ];

      evaluateWith =
        hostName: module: index: extra:
        mkNode {
          imports = [
            cfg.defaults
            module
            peersModule
          ]
          ++ extra;
          _module.args.nodes = nodes;
          # For `vivarium.lateClosure`: a guest names the settings paths it reads.
          _module.args.settings = cfg.settings;
          networking.hostName = lib.mkDefault hostName;
          vivarium.sshPort = lib.mkDefault (4325 + index);
          vivarium.backend = lib.mkDefault cfg.backend;
          vivarium.index = index;
          vivarium.segments = cfg.segments;
        };

      /*
        Every guest, evaluated, by name. Each one receives all of them as
        the module argument `nodes`, as in nixos-test, so a guest may read
        static facts about its peers: a name, an address, a secret written
        in Nix. Lazy, so a guest reading a peer's address is no cycle.
      */
      /*
        From the unchecked `cfg`: the run's assertions read its phases, and
        a recipe may derive a phase from these guests.
      */
      evaluated =
        lib.mapAttrs evaluate cfg.nodes
        // lib.optionalAttrs (routerEnabled && !(cfg.nodes ? ${routerName})) {
          ${routerName} = evaluate routerName { };
        };

      nodes = lib.mapAttrs (_: node: node.config) evaluated;

      # Each template evaluated once, as a node is. Its index here only
      # keeps it apart from the nodes at evaluation; an instance gets its
      # own from the runner.
      templates = lib.listToAttrs (
        lib.imap0 (
          ordinal: name:
          lib.nameValuePair name
            (mkNode {
              imports = [
                cfg.defaults
                cfg.templates.${name}
                peersModule
              ];
              _module.args.nodes = nodes;
              _module.args.settings = cfg.settings;
              networking.hostName = lib.mkDefault name;
              vivarium.sshPort = lib.mkDefault (4325 + lib.length (lib.attrNames cfg.nodes) + ordinal);
              vivarium.backend = lib.mkDefault cfg.backend;
              vivarium.index = lib.length (lib.attrNames cfg.nodes) + ordinal;
              vivarium.template = true;
              vivarium.segments = cfg.segments;
            }).config
        ) (lib.attrNames cfg.templates)
      );

      templateSegments = lib.unique (
        lib.concatMap (template: map (nic: nic.segment) template.vivarium.nics) (lib.attrValues templates)
      );

      machines = lib.attrValues nodes;

      # Fixed host ports two `vivarium.inbound` entries of the run share.
      # The second bind would fail the boot; evaluation names both first.
      # A null address is 127.0.0.1 on macOS, so it clashes with every
      # address, as 0.0.0.0 does.
      inboundClashes =
        let
          wildcard = address: address == null || address == "0.0.0.0";
          fixed = lib.concatMap (
            machine:
            lib.concatMap (
              entry:
              lib.optionals (entry.host != null) (
                map (protocol: {
                  node = machine.networking.hostName;
                  inherit protocol;
                  inherit (entry) host address;
                }) entry.protocols
              )
            ) machine.vivarium.inbound
          ) machines;
          clash =
            a: b:
            a.protocol == b.protocol
            && a.host == b.host
            && (wildcard a.address || wildcard b.address || a.address == b.address);
          show =
            each:
            "${each.node} (${toString each.host}/${each.protocol}${
              lib.optionalString (each.address != null) " on ${each.address}"
            })";
        in
        lib.concatLists (
          lib.imap0 (
            index: a: map (b: "${show a} and ${show b}") (lib.filter (clash a) (lib.drop (index + 1) fixed))
          ) fixed
        );

      # Guests whose uplink only the router serves, in a run without one.
      # `uplink.enable` is the appliance's, and false leaves out vec0.
      unrouted = map (machine: machine.networking.hostName) (
        lib.filter (
          machine:
          machine.vivarium.uplink.proxy == "router"
          && machine.vivarium.uplink.segment == null
          && (machine.vivarium.uplink.enable or true)
        ) machines
      );

      pythonPackages = lib.unique (
        checkedConfig.python.packages ++ lib.concatMap (machine: machine.vivarium.python.packages) machines
      );

      # `cli` itself when nothing adds a package, so most runs share it.
      runVenv =
        if pythonPackages == [ ] then
          cli
        else
          nixvenv {
            python = pkgs.python3;
            packages = _: [ session ] ++ pythonPackages;
            executables = [ "vivarium-runner" ];
          };

      # Named in the builder rather than added to it: naming a store path
      # is what makes Nix build it, so the check runs before the guests
      # do. Every phase's script, not one -- a recipe that does not type
      # check is a recipe that breaks its consumers.
      checked =
        let
          typing = checkedConfig.typeCheck;
        in
        lib.optionalString typing.enable "${typeCheck {
          name = "${name}-phases";
          scripts = map (
            phase: if phase.pytest != null then phase.pytest.tests else phase.script
          ) checkedConfig.ordered;
          extraPaths = checkedConfig.pythonPath;
          extraPackages = pythonPackages;
          inherit (typing) strict ignore;
        }}";

      spec = pkgs.writeText "vivarium-${name}-spec.json" (
        builtins.toJSON {
          inherit (checkedConfig) name settings;
          # The runner's half of a segment; its MTU is on each interface.
          # A capture needs the frames, so it forces the hub.
          # An instance joins a template's segment while frames flow, and
          # only a hub takes a port then.
          segments =
            lib.mapAttrs (name: segment: {
              inherit (segment) capture instanceAddresses;
              hub = segment.hub || segment.capture || lib.elem name templateSegments;
            }) checkedConfig.segments
            // lib.genAttrs (lib.subtractLists (lib.attrNames checkedConfig.segments) templateSegments) (_: {
              capture = false;
              instanceAddresses = [ ];
              hub = true;
            });
          templates = map (
            template:
            machineSpec template
            // lib.optionalAttrs (!template.vivarium.hostStore.enable && template.vivarium.closure != null) {
              storePaths = "${template.vivarium.closure}/store-paths";
            }
          ) (lib.attrValues templates);
          unshare = "${lib.getBin pkgs.util-linux}/bin/unshare";
          # The runner that knows every field below. No cycle: the
          # package does not depend on any spec.
          vivarium = "${runVenv}";
          pythonPath = map (path: "${path}") checkedConfig.pythonPath;
          knobs = checkedConfig.resolved;
          phases = map (
            phase:
            {
              inherit (phase)
                name
                after
                always
                nodes
                ;
            }
            // lib.optionalAttrs (phase.timeout != null) { inherit (phase) timeout; }
            // lib.optionalAttrs phase.nodesFromDependents { nodesFromDependents = true; }
            // (
              if phase.pytest != null then
                {
                  pytest = {
                    tests = "${phase.pytest.tests}";
                    inherit (phase.pytest) args;
                  };
                }
              else
                { script = "${phase.script}"; }
            )
          ) checkedConfig.ordered;
          # Each guest's closure, which the runner turns into its store
          # view. The same closureInfo its Nix database is loaded from.
          # Not for `vivarium.hostStore`, whose point is the host's whole
          # store and its database under the guest's own. Nor for a
          # guest that never sees the store (`vivarium.closure = null`).
          machines =
            if inboundClashes != [ ] then
              throw "vivarium.inbound: one fixed host port, asked for twice: ${lib.concatStringsSep "; " inboundClashes}. Give one another port, or none to take any free one."
            else if unrouted != [ ] then
              throw "${lib.concatStringsSep ", " unrouted}: only the run's router serves the uplink of a guest that is not NixOS, and this run has none. Leave router.enable null, set the guest's vivarium.uplink.segment, or leave its uplink out."
            else
              map (
                machine:
                let
                  inherit (machine.vivarium) closure;
                in
                machineSpec machine
                # Not in the guest's root image: a late path is often a source
                # tree, and an image that held it rebuilt for every edit
                # (nixidae/nixidae#1182).
                # Not a guest assertion: a late path may be a system built from
                # this guest, and its toplevel checks every assertion.
                // lib.optionalAttrs (machine.vivarium.lateClosure != [ ]) {
                  lateClosure =
                    if closure == null then
                      throw "${machine.networking.hostName}: vivarium.lateClosure is set, but this guest (kind ${machine.vivarium.kind}) has no store to add it to."
                    else
                      "${pkgs.closureInfo { rootPaths = machine.vivarium.lateClosure; }}";
                }
                // lib.optionalAttrs (!machine.vivarium.hostStore.enable && closure != null) {
                  storePaths = "${closure}/store-paths";
                }
                // lib.optionalAttrs (machine.vivarium.requires != [ ]) {
                  requires =
                    let
                      names = map (each: each.networking.hostName) machines;
                      unknown = lib.subtractLists names machine.vivarium.requires;
                    in
                    if unknown == [ ] then
                      machine.vivarium.requires
                    else
                      throw "${machine.networking.hostName}: vivarium.requires names ${lib.concatStringsSep ", " unknown}, which are not guests of this run (${lib.concatStringsSep ", " names}).";
                }
              ) machines;
        }
      );

      vivarium = lib.getExe runVenv;

      /*
        The run outside the sandbox.

        `--out` is required and not defaulted. `lib.nix` has held since
        the beginning that a run by hand records only where it is told
        to, so that nothing writes to a directory nobody chose -- and
        being told is now cheap, because it is one flag rather than an
        environment variable nobody remembers.
      */
      /*
        `vivarium-runner` with this run's spec and some flags baked in: a binary
        wrapper, no shell. The type check is named in its environment,
        which makes it a dependency: the by-hand door is where a type
        error gets written, so it must not skip the check.

        Local, because the spec's closure holds the root images: a remote
        builder must take each one first, at its whole disk size.
      */
      wrap =
        program: command: flags:
        pkgs.runCommandLocal program
          {
            nativeBuildInputs = [ pkgs.makeBinaryWrapper ];
            meta.mainProgram = program;
          }
          ''
            makeWrapper ${vivarium} $out/bin/${program} \
              --add-flags ${lib.escapeShellArg "${command} --spec ${spec} ${flags}"} \
              --set VIVARIUM_TYPECHECKED ${lib.escapeShellArg checked}
          '';

      # A run by hand: a failed phase skips what depends on it, as in the check.
      driver = wrap "vivarium-driver-${name}" "run" "";
      # The same, paused on the first failure with the guests up. The MCP
      # server starts this one.
      driverDebug = wrap "vivarium-driver-debug-${name}" "run" "--break-on-failure";
      # nixos-test's REPL: paused before the first phase; see vivarium/repl.py.
      # `.driverInteractive` is this, from the run with `interactive`
      # merged in.
      driverInteractiveHere = wrap "vivarium-driver-interactive-${name}" "run" "--interactive";
      # An environment: ready after the last phase, guests up until `ctl stop`.
      up = wrap "vivarium-up-${name}" "run" "--hold";
      lister = wrap "vivarium-phases-${name}" "phases" "";

      extend =
        { modules }:
        mkTest {
          imports = [ module ] ++ modules;
        };
      # Every guest on one backend, whatever its own configuration says.
      onBackend =
        backend:
        extend {
          modules = [
            {
              defaults.vivarium.backend = lib.mkForce backend;
              # The router runs on QEMU or krun too where the guests do, and
              # on UML elsewhere.
              router.backend = lib.mkIf (lib.elem backend [
                "qemu"
                "krun"
              ]) (lib.mkForce backend);
            }
          ];
        };

      /*
        The run inside one, which never fails.

        Nix deletes the output of a derivation that fails, so a run that
        reports failure by failing throws away the evidence of the one run
        anybody wanted to read. This always succeeds and writes what
        happened to `status`; the check below fails, and reads nothing but
        that file.

        The same `vivarium run` the developer gets, with `--out` pointed at the
        derivation's own output. Nothing branches on being in a sandbox,
        which is what stops the two drifting. `--no-control` is the one
        flag it adds: nobody can reach in here, and a socket left in
        `$out` by a killed run would fail the output.
      */
      probe = probeFor machines;
      # In the name, so a build log tells `checks.lan.qemu` from `checks.lan`.
      backends = lib.concatStringsSep "-" (
        lib.unique (lib.sort lib.lessThan (map (machine: machine.vivarium.backend) machines))
      );
      attempt =
        pkgs.runCommand "vivarium-session-${name}-${backends}-attempt"
          (
            {
              requiredSystemFeatures = featuresFor machines;
              # The guests run where the build was asked for, so `report.json`
              # times that machine. A UML run asks for no feature, and without
              # this a remote builder takes it.
              preferLocalBuild = true;
              passthru = { inherit spec probe; };
            }
            # A forward binds 127.0.0.1, which the darwin sandbox refuses.
            // lib.optionalAttrs pkgs.stdenv.hostPlatform.isDarwin { __darwinAllowLocalNetworking = true; }
          )
          ''
            export HOME="$TMPDIR"
            mkdir -p "$out"
            echo "phases checked: ${checked}" > "$out/typecheck"
            # A dependency, so a sandbox that cannot run a container guest
            # fails there, in seconds and by name, before this boots one.
            ${lib.optionalString (probe != null) ''cp ${probe} "$out/probe"''}
            ${vivarium} run --spec ${spec} --out "$out" --no-control --random-ports || true
            test -f "$out/status" || echo 1 > "$out/status"
          '';
    in
    pkgs.runCommand "vivarium-session-${name}"
      {
        # It reads one file. A remote builder takes the attempt's closure
        # first, root images and all, at their whole disk size.
        preferLocalBuild = true;
        passthru = {
          inherit
            attempt
            spec
            nodes
            ;
          systems = evaluated;
          inherit
            driver
            driverDebug
            extend
            driverInteractiveHere
            up
            ;
          driverInteractive = (extend { modules = [ checkedConfig.interactive ]; }).driverInteractiveHere;
          uml = onBackend "uml";
          qemu = onBackend "qemu";
          krun = onBackend "krun";
          container = onBackend "container";
          phases = lister;
          config = checkedConfig;
        };
      }
      (
        readVerdict attempt [
          "log=everything, readable"
          "events.jsonl=everything, for a machine"
          "console=one file per guest"
          "phases.json=what each phase did"
          "junit.xml=one case per test"
          "report.json=timings"
          "artifacts=what each guest wrote to /artifacts"
        ]
      );
}

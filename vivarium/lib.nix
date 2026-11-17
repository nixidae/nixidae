# mkNode and mkTest, as a library.
#
# These used to live in `flake.nix`'s `let`, where nothing outside this
# repository could reach them. A caller with its own guests and its own script
# is the point of the exercise -- easykubenix drives `ekn kubeapply` against a
# cluster this builds -- so they are a file that takes a package set.
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
in
rec {
  inherit guestPkgs;

  /*
    The library a test script imports, reachable without evaluating a
    guest: a caller puts it in the Python it type checks with.  It carries
    `py.typed`, so a type checker reads `vms.node` as a `Machine`.

        nixvenv { python = pkgs.python3; packages = _: [ vivarium.runner ]; }
  */
  runner = pkgs.callPackage ./pkgs/vivarium-runner { };

  /**
    The session, and the `vivarium` CLI that drives one.

    The redesign lives here; `runner` above is the mechanism it uses and
    is not going away. See `docs/design/history/running-anywhere.md`.
  */
  session = pkgs.callPackage ./pkgs/vivarium { vivarium-runner = runner; };

  # `vivarium_k8s`, the cluster library. `vivarium.recipes.kubernetes` puts
  # it in the run's venv.
  k8s = pkgs.callPackage ./pkgs/vivarium-k8s { vivarium-runner = runner; };

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
      [ value ]
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

  /*
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
        # One run per script: two scripts may share a basename --
        # `recipes/boot.py` and `tests/phases/boot.py` do -- and one run
        # refuses two modules of the same name.
        ${lib.concatMapStrings (script: ''
          zuban mypy --config-file ${config} --python-executable ${python}/lib/venv/bin/python3 ${script} \
            || failed=1
        '') scripts}
        if [ -n "''${failed:-}" ]; then
          echo "the scripts above do not type check against vivarium_runner" >&2
          exit 1
        fi
        touch $out
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
      router = machine.vivarium.router.enable;
      forward = machine.vivarium.forward;
      inherit (machine.vivarium) control;
    }
    // lib.optionalAttrs (machine.vivarium.uplink.proxy == "router") {
      uplinkAddress = machine.vivarium.router.address machine.vivarium.index;
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
    }
    // lib.optionalAttrs (
      lib.elem machine.vivarium.backend [
        "qemu"
        "krun"
      ]
      && machine.vivarium.kind == "nixos"
    ) { boot = machine.system.build.qemuBoot; }
    // lib.optionalAttrs (machine.vivarium.backend == "container") {
      boot = machine.system.build.containerBoot;
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

  # A guest: an ordinary NixOS configuration plus ./modules.
  #
  # `eval-config.nix` and not `lib.nixosSystem`. That name only exists on the
  # lib a flake gets from nixpkgs' own `flake.nix`; `pkgs.lib` is the library
  # itself and has never carried it. This is what the flake wrapper calls, and
  # `system = null` is how it says that `nixpkgs.pkgs` below decides the
  # platform.
  mkNode =
    module:
    import (guestPkgs.path + "/nixos/lib/eval-config.nix") {
      inherit lib;
      system = null;
      # What the host runs for a guest: its launcher, passt, the egress.
      specialArgs.hostPkgs = pkgs;
      modules = [
        ./modules
        { nixpkgs.pkgs = guestPkgs; }
        module
      ];
    };

  # nixos-test's shape, mapped onto mkTest; see nixos-test.nix.
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
    pkgs.runCommand "vivarium-pytest-${name}" { passthru = { inherit attempt; }; } ''
      echo "the run is at ${attempt}: log, junit.xml, runs/"
      status=$(cat ${attempt}/status)
      if [ "$status" != 0 ]; then
        echo "--- the last 50 lines of ${attempt}/log ---"
        tail -n 50 ${attempt}/log
        echo "--- end ---"
        echo "pytest failed (exit $status)" >&2
        exit 1
      fi
      mkdir -p $out
      ln -s ${attempt} $out/attempt
    '';

  # The address and key of `vivarium.recipes.cache`, for what a run renders
  # outside its own evaluation.
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
        ];
        specialArgs = {
          # A run's modules mostly write guests.
          pkgs = guestPkgs;
          hostPkgs = pkgs;
          # The Python packages vivarium builds, for a recipe's
          # `python.packages`.
          vivariumPackages = { inherit runner session k8s; };
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

      failed = lib.filter (each: !each.assertion) (
        cfg.assertions
        ++ [
          {
            assertion = unjoined == [ ];
            message = "segments: no guest joins ${lib.concatStringsSep ", " unjoined}; an interface names its segment in vivarium.interfaces.<name>.segment.";
          }
        ]
      );
      checkedConfig =
        if failed == [ ] then cfg else throw (lib.concatMapStringsSep "\n" (each: each.message) failed);

      inherit (checkedConfig) name;

      settingsFile = pkgs.writeText "vivarium-${name}-settings.json" (
        builtins.toJSON checkedConfig.settings
      );

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
      # The router's index is its own, so adding it moves no guest's
      # index, MAC or ssh port. The highest: a MAC octet is one byte.
      guestNames = lib.filter (hostName: !(cfg.router.enable && hostName == cfg.router.name)) (
        lib.attrNames cfg.nodes
      );
      indexOf =
        hostName:
        if cfg.router.enable && hostName == cfg.router.name then
          254
        else
          lib.lists.findFirstIndex (name: name == hostName) null guestNames;

      evaluated = lib.listToAttrs (
        map (
          hostName:
          let
            index = indexOf hostName;
          in
          lib.nameValuePair hostName (mkNode {
            imports = [
              cfg.defaults
              cfg.nodes.${hostName}
              peersModule
            ];
            _module.args.nodes = nodes;
            # For `vivarium.lateClosure`: a guest names the settings paths it reads.
            _module.args.settings = cfg.settings;
            networking.hostName = lib.mkDefault hostName;
            vivarium.sshPort = lib.mkDefault (4325 + index);
            vivarium.backend = lib.mkDefault cfg.backend;
            vivarium.index = index;
            vivarium.segments = cfg.segments;
          })
        ) (lib.attrNames cfg.nodes)
      );

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

      machines = map (hostName: nodes.${hostName}) (lib.attrNames checkedConfig.nodes);

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
          machines = map (
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

      # A run by hand that exits on the first failure, as the check does.
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
      attempt =
        pkgs.runCommand "vivarium-session-${name}-attempt"
          {
            requiredSystemFeatures = featuresFor machines;
            passthru = { inherit spec probe; };
          }
          ''
            export HOME="$TMPDIR"
            mkdir -p "$out"
            echo "phases checked: ${checked}" > "$out/typecheck"
            # A dependency, so a sandbox that cannot run a container guest
            # fails there, in seconds and by name, before this boots one.
            ${lib.optionalString (probe != null) ''cp ${probe} "$out/probe"''}
            ${vivarium} run --spec ${spec} --out "$out" --no-control || true
            test -f "$out/status" || echo 1 > "$out/status"
          '';
    in
    pkgs.runCommand "vivarium-session-${name}"
      {
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
      ''
        echo "the run is at ${attempt}"
        echo "  log:       ${attempt}/log            everything, readable"
        echo "  events:    ${attempt}/events.jsonl   everything, for a machine"
        echo "  console:   ${attempt}/console/       one file per guest"
        echo "  phases:    ${attempt}/phases.json    what each phase did"
        echo "  junit:     ${attempt}/junit.xml"
        echo "  timings:   ${attempt}/report.json"
        echo "  artifacts: ${attempt}/artifacts"

        status=$(cat ${attempt}/status)
        if [ "$status" != 0 ]; then
          echo
          echo "--- the last 50 lines of ${attempt}/log ---"
          tail -n 50 ${attempt}/log
          echo "--- end ---"
          echo
          echo "the run failed (exit $status); the paths above hold what it left" >&2
          exit 1
        fi

        mkdir -p $out
        ln -s ${attempt} $out/attempt
        # Named in a loop rather than one line each: a sink added to the
        # runner should not need an edit here to be reachable from
        # `result/`, and one that was forgotten is invisible.
        for each in log events.jsonl console phases.json junit.xml report.json artifacts; do
          ln -s ${attempt}/"$each" "$out/$each"
        done
      '';
}

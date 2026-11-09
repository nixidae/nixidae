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

  k8sImages = pkgs.callPackage ./modules/k8s-images.nix { };
  tests = import ./tests {
    inherit
      pkgs
      lib
      sources
      nixvenv
      vivarium
      k8sImages
      ;
  };
  inherit (tests) k8sConfig;

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
    huggorm), `vivarium-mcp`, the agent skill at
    `share/skills/vivarium/vivarium`, and the Claude Code mod at
    `share/claude-plugins/vivarium-runs`. Not the runner: a run names its own,
    and every command it prints is absolute. Not inside `cli`: every
    test's spec names `cli`, so a skill inside it would rebuild every test
    on each edit.
  */
  tools = pkgs.runCommand "vivarium-tools" { meta.mainProgram = "vivarium"; } ''
    mkdir -p $out/bin $out/share/skills/vivarium $out/share/claude-plugins
    ln -s ${vivarium-eval}/bin/vivarium ${vivarium-eval}/bin/vivarium-mcp $out/bin/
    cp -r ${./skill/vivarium} $out/share/skills/vivarium/vivarium
    cp -r ${mod} $out/share/claude-plugins/vivarium-runs
  '';
  # Not the types Claude Code writes into `.claude-plugin/types` on each load.
  mod = lib.fileset.toSource {
    root = ./claude-plugin;
    fileset = lib.fileset.unions [
      ./claude-plugin/.claude-plugin/plugin.json
      ./claude-plugin/hooks
      ./claude-plugin/types
      ./claude-plugin/README.md
    ];
  };
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
  inherit (tests) checks;

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
    # `nix run --file . packages.vivarium-mcp`: what `.mcp.json` starts.
    vivarium-mcp = lib.addMetaAttrs { mainProgram = "vivarium-mcp"; } vivarium-eval;
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
    inherit (tests.internal)
      demo
      store
      k8s-pull
      uplink
      incr
      ;
    inherit (tests.internal.demo.config.system.build) vivariumRunner vivariumRootImage toplevel;

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
      exec ${tests.internal.demo.config.system.build.vivariumRunner}/bin/vivarium-run --command speedtest-cli "$@"
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
      python3 ${./tests/mcp_driver.py} ${vivarium-eval}/bin/vivarium-mcp ${tests.checks.pytest-phase.session.spec}
      touch $out
    '';
  };
}

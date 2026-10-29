let
  compat = import ./nix/compat.nix;
in
{
  inputs ? compat,
  system ? builtins.currentSystem,
  pkgs ? import inputs.nixpkgs {
    inherit system;
    # nixkube asks for this, and so does nanopynix. One package set for
    # everybody means the root has to ask for it too, or those two would each
    # need a set of their own and the sharing would be gone.
    config.allowUnfree = true;
  },
}:
rec {
  # Every source, as a directory. Each one takes this and imports what it
  # wants. nix/wire.nix is the one implementation, and a source's own
  # nix/sources.nix calls it too.
  sources = import ./nix/wire.nix { inherit inputs; };

  # Read a flake we do not own, with these sources in place of its own lock.
  callFlake = import ./nix/call-flake.nix { inherit sources; };

  shell = pkgs.callPackage ./pkgs/shell { };

  # Write each source's flake.lock, and export each source to its own
  # repository. .github/workflows/josh-export.yml runs both.
  lock-subtrees = pkgs.callPackage ./pkgs/lock-subtrees { };
  josh-export = pkgs.callPackage ./pkgs/josh-export { };
  # The josh josh-export runs, for a job that needs a source's own commit.
  inherit (pkgs) josh;
  # The ruff pyedit.toml fixes staged Python with.
  inherit (pkgs) ruff;

  # -- the sources -----------------------------------------------------------
  #
  # Each one is also a repository somebody can build on its own, exported by
  # josh. A source called here gets the arguments its own default.nix would
  # give itself, so the two builds are the same derivations.
  pynixd = import sources.pynixd { inherit pkgs sources; };

  nanopynix = import sources.nanopynix { inherit pkgs sources system; };

  easykubenix = import sources.easykubenix { inherit pkgs sources system; };

  # The kr8s apply engine easykubenix's `ekn` builds on.
  kappy = import sources.kappy { inherit pkgs sources system; };

  # It builds its own package set, because it applies an overlay of its own,
  # so it takes the sources rather than the set.
  nixkube = import sources.nixkube { inherit sources system; };

  # NixOS integration tests on User-Mode Linux, so a guest is a process and a
  # test is a derivation that passes or fails.
  vivarium = import sources.vivarium { inherit pkgs sources; };

  # huggorm and the two built on it take a package set of their own, with no
  # config: their flakes build the same one, and a plain `import nixpkgs { }`
  # would read ~/.config/nixpkgs.
  huggormPkgs = import inputs.nixpkgs {
    inherit system;
    config = { };
    overlays = [ ];
  };
  huggorm = import sources.huggorm {
    inherit sources;
    pkgs = huggormPkgs;
  };

  pydd = import sources.pydd {
    inherit sources;
    pkgs = huggormPkgs;
    huggormSet = huggorm;
  };

  havsorm = import sources.havsorm {
    inherit sources huggorm;
    pkgs = huggormPkgs;
  };

  # Every output that links huggorm, once for each lane: huggorm's lanes, plus
  # `stable` and `latest`, which nanopynix names. `lanes.determinate.ekn` is
  # the `ekn` that evaluates with Determinate Nix. nixidae/nixidae#1108.
  #
  # Not here: pydd picks its lanes for their features, pynixd ships no
  # huggorm, and kappy's closure holds none.
  lanes = pkgs.lib.mapAttrs (name: lane: {
    inherit (lane) pynix pynix-lsp;
    inherit (easykubenix.internal.lanes.${name}) ekn;
    inherit
      ((import sources.havsorm {
        inherit sources huggorm;
        pkgs = huggormPkgs;
        huggormPackage = lane.huggorm;
      }).packages
      )
      havsorm
      ;
  }) nanopynix.lanes;

  ghanix = import sources.ghanix { inherit pkgs sources; };

  nixvenv = import sources.nixvenv { inherit pkgs; };

  # The test workflows of every source, rendered for this repository.
  # nix/ci.nix says how.
  ci = import ./nix/ci.nix {
    inherit (pkgs) lib;
    inherit pkgs ghanix;
    inherit (nixvenv.lib) nixvenv;
    sets = {
      inherit
        easykubenix
        havsorm
        huggorm
        kappy
        nanopynix
        nixkube
        nixvenv
        pydd
        pynixd
        vivarium
        ;
    };
  };
  ci-workflow-update = ci.update;

  # The files every source shares. nix/shared-config.nix.
  sharedConfig = import ./nix/shared-config.nix {
    inherit (pkgs) lib;
    inherit pkgs;
    sources = [
      "easykubenix"
      "havsorm"
      "huggorm"
      "kappy"
      "nanopynix"
      "nixkube"
      "nixvenv"
      "pydd"
      "pynixd"
      "vivarium"
    ];
    wired = [
      "easykubenix"
      "havsorm"
      "huggorm"
      "kappy"
      "nanopynix"
      "nixkube"
      "pydd"
      "pynixd"
      "vivarium"
    ];
    documented = [
      "easykubenix"
      "nanopynix"
      "nixkube"
      "nixvenv"
      "pydd"
      "pynixd"
    ];
  };
  shared-config-update = sharedConfig.update;

  # The one Nix formatter. nix/nixfmt.nix.
  nixFormat = import ./nix/nixfmt.nix {
    inherit (pkgs) lib;
    inherit pkgs;
  };
  nixfmt-update = nixFormat.update;

  # The one Nix linter. nix/statix.nix.
  nixLint = import ./nix/statix.nix {
    inherit (pkgs) lib;
    inherit pkgs;
  };
  statix-fix = nixLint.fix;

  checks = {
    # That every tracked .nix file is formatted.
    nixfmt = nixFormat.check;

    # That statix finds nothing in any tracked .nix file.
    statix = nixLint.check;

    # That every committed `<source>-*.yml` is what its source renders.
    ciWorkflows = ci.check;

    # That every source's copy of nix/shared/ is the same bytes.
    sharedConfig = sharedConfig.check;

    # That calling a flake we do not own uses our inputs and not its lock.
    # nix/examples/call-flake.nix is the worked example as well as the gate.
    callFlake = pkgs.callPackage ./nix/examples/call-flake.nix {
      inherit sources pkgs callFlake;
    };

    licenses = pkgs.callPackage ./nix/licenses.nix { inherit sources; };

    pythonpath = pkgs.callPackage ./nix/pythonpath.nix { inherit sources; };

    mypyIni = pkgs.callPackage ./nix/mypy-ini.nix { inherit sources; };
  };
}

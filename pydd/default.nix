{
  sources ? import ./nix/sources.nix,
  huggormSet ? import sources.huggorm { inherit sources; },
  # huggorm's package set, so the bindings and the planners share one Python.
  pkgs ? huggormSet.pkgs,
}:
let
  inherit (pkgs) lib;

  # builder-rpc-v0 is on Nix master and in no release.
  nix = pkgs.nixVersions.git;

  # The huggorm lane whose Nix has builder-rpc-v0.
  huggorm = huggormSet.lanes.git.huggorm;

  pydd = pkgs.callPackage ./nix/planner.nix { };

  # `pydd` with `writer` as the default, so each example is written once.
  withWriter =
    writer:
    let
      self = pydd // {
        mkPlanner = args: pydd.mkPlanner ({ inherit writer huggorm nix; } // args);
        python = pkgs.callPackage ./nix/python.nix { pydd = self; };
        evaluate = pkgs.callPackage ./nix/evaluate.nix { pydd = self; };
      };
    in
    self;

  examplesFor =
    writer:
    lib.mapAttrs (name: _: pkgs.callPackage ./examples/${name} { pydd = withWriter writer; }) (
      lib.filterAttrs (_: type: type == "directory") (builtins.readDir ./examples)
    );
in
rec {
  inherit pkgs pydd huggorm;

  # nix run --file . run -- examples.cli.hello
  # nix run --file . run -- examples.huggorm.venv check
  examples = {
    cli = examplesFor "cli";
    huggorm = examplesFor "huggorm";
  };

  # Build an example's output in a private store, because a daemon older
  # than Nix master refuses `builder-rpc-v0`. The store keeps /nix/store as
  # its store directory, so the cache substitutes into it.
  run = pkgs.writeShellApplication {
    name = "pydd-run";
    runtimeInputs = [ nix ];
    text = ''
      attr=''${1:?usage: pydd-run <example attribute> [out|check] [nix build flags]}
      target=''${2:-out}
      shift $(( $# < 2 ? $# : 2 ))
      store=''${PYDD_STORE:-''${XDG_CACHE_HOME:-$HOME/.cache}/pydd/store}
      export NIX_CONFIG="extra-experimental-features = nix-command ca-derivations dynamic-derivations
      use-cgroups = false"

      # What the planner runs on may be built here and nowhere else, such
      # as a huggorm lane, so copy it in rather than build it again.
      closure=$(nix build --file . "$attr.closure" --no-link --print-out-paths)
      nix copy --to "$store" --no-check-sigs "$closure"

      out=$(nix build --store "$store" --file . "$attr.$target" \
        --system-features builder-rpc-v0 --no-link --print-out-paths -L "$@")
      echo "$out" >&2
      nix store cat --store "$store" "$out"
    '';
  };

  checks = {
    tests = pkgs.callPackage ./nix/tests.nix { inherit huggorm; };
  };
}

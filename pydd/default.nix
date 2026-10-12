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

  pydd = pkgs.callPackage ./lib { inherit huggorm nix; };

  # The evaluator in the planner is huggorm's.
  writersOf =
    name:
    if name == "evaluated" || name == "nested" then
      [ "huggorm" ]
    else
      [
        "cli"
        "huggorm"
      ];
in
rec {
  inherit pkgs pydd huggorm;

  # nix run --file . run -- examples.hello.cli
  # nix run --file . run -- examples.venv.huggorm check
  examples = lib.mapAttrs (
    name: _:
    lib.genAttrs (writersOf name) (
      writer:
      pkgs.callPackage ./examples/${name} {
        pydd = pkgs.callPackage ./lib { inherit huggorm nix writer; };
      }
    )
  ) (lib.filterAttrs (_: type: type == "directory") (builtins.readDir ./examples));

  # Build an example's output in a private store, because a daemon older
  # than Nix master refuses `builder-rpc-v0`. The store keeps /nix/store as
  # its store directory, so the cache substitutes into it.
  run = pkgs.writeShellApplication {
    name = "pydd-run";
    runtimeInputs = [
      nix
      pkgs.jq
    ];
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
      if [[ $(nix store ls --store "$store" --json "$out" | jq -r .type) == regular ]]; then
        nix store cat --store "$store" "$out"
      else
        nix store ls --store "$store" "$out"
      fi
    '';
  };

  checks = {
    tests = pkgs.callPackage ./nix/tests.nix { inherit huggorm; };
  };
}

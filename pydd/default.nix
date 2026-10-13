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

  # The newest release, for planners that write `$out`. Its huggorm lane
  # renders the ATerm that this Nix parses.
  nixReleased = pkgs.nixVersions.nix_2_35;
  huggormReleased = huggormSet.lanes.nix_2_35.huggorm;

  # The evaluator in the planner is huggorm's, and so is the staging store of
  # a planner that writes `$out`.
  writersOf =
    name:
    if
      lib.elem name [
        "evaluated"
        "nested"
        "released"
      ]
    then
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
        pydd = pkgs.callPackage ./lib (
          if name == "released" then
            {
              inherit writer;
              huggorm = huggormReleased;
              nix = nixReleased;
            }
          else
            { inherit huggorm nix writer; }
        );
      }
    )
  ) (lib.filterAttrs (_: type: type == "directory") (builtins.readDir ./examples));

  # Build an example's output in a private store with a given Nix, because
  # the daemon of this machine may be neither Nix master nor a release. The
  # store keeps /nix/store as its store directory, so the cache substitutes
  # into it.
  mkRun =
    {
      name,
      nix,
      store,
      features,
    }:
    pkgs.writeShellApplication {
      inherit name;
      runtimeInputs = [
        nix
        pkgs.jq
      ];
      text = ''
        attr=''${1:?usage: ${name} <example attribute> [out|check] [nix build flags]}
        target=''${2:-out}
        shift $(( $# < 2 ? $# : 2 ))
        store=''${PYDD_STORE:-''${XDG_CACHE_HOME:-$HOME/.cache}/pydd/${store}}
        export NIX_CONFIG="extra-experimental-features = nix-command ca-derivations dynamic-derivations
        use-cgroups = false"

        # What the planner runs on may be built here and nowhere else, such
        # as a huggorm lane, so copy it in rather than build it again.
        closure=$(nix build --file . "$attr.closure" --no-link --print-out-paths)
        nix copy --to "$store" --no-check-sigs "$closure"

        out=$(nix build --store "$store" --file . "$attr.$target" \
          --system-features "${features}" --no-link --print-out-paths -L "$@")
        echo "$out" >&2
        if [[ $(nix store ls --store "$store" --json "$out" | jq -r .type) == regular ]]; then
          nix store cat --store "$store" "$out"
        else
          nix store ls --store "$store" "$out"
        fi
      '';
    };

  # nix run --file . run -- examples.hello.cli
  run = mkRun {
    name = "pydd-run";
    inherit nix;
    store = "store";
    features = "builder-rpc-v0";
  };

  # A stock release, which has no `builder-rpc-v0`.
  # nix run --file . run-released -- examples.released.huggorm
  run-released = mkRun {
    name = "pydd-run-released";
    nix = nixReleased;
    store = "store-${nixReleased.version}";
    features = "";
  };

  checks = {
    tests = pkgs.callPackage ./nix/tests.nix { inherit huggorm; };
  };

  # The uv projects of the ddrn lab, each with its check, once for each
  # writer. The writer chooses the path from plan to derivations.
  # nix run --file . run -- projects.cli.pure-app
  projects = lib.genAttrs [ "cli" "huggorm" ] (
    writer:
    import ./tests/projects {
      inherit pkgs;
      pydd = pkgs.callPackage ./lib { inherit huggorm nix writer; };
    }
  );

  # Run every check of `projects` with both writers, and compare the two
  # environments. `graph.nix` and `pydd.python._render` must give the same
  # content-addressed path. A check needs `builder-rpc-v0`, so it builds in
  # the private store of `run`, and no `checks` attribute can hold it.
  test-projects = pkgs.writeShellApplication {
    name = "pydd-test-projects";
    runtimeInputs = [
      run
      nix
    ];
    text = ''
      log=''${PYDD_LOG:-''${XDG_CACHE_HOME:-$HOME/.cache}/pydd/projects}
      store=''${PYDD_STORE:-''${XDG_CACHE_HOME:-$HOME/.cache}/pydd/store}
      export NIX_CONFIG="extra-experimental-features = nix-command ca-derivations dynamic-derivations
      use-cgroups = false"
      mkdir -p "$log"
      if (( $# > 0 )); then
        names=("$@")
      else
        read -ra names <<< "$(nix eval --raw --file . projects.cli --apply 'p: toString (builtins.attrNames p)')"
      fi
      failed=0
      for name in "''${names[@]}"; do
        paths=()
        for writer in cli huggorm; do
          if pydd-run "projects.$writer.$name" > "$log/$name-$writer.log" 2>&1; then
            echo "pass  $name ($writer)"
            paths+=("$(nix build --store "$store" --file . "projects.$writer.$name.environment" \
              --system-features builder-rpc-v0 --no-link --print-out-paths 2>> "$log/$name-$writer.log")")
          else
            echo "FAIL  $name ($writer)  ($log/$name-$writer.log)"
            failed=1
          fi
        done
        if (( ''${#paths[@]} == 2 )); then
          if [[ ''${paths[0]} == "''${paths[1]}" ]]; then
            echo "same  $name  ''${paths[0]}"
          else
            echo "DIFFER  $name  cli ''${paths[0]}  huggorm ''${paths[1]}"
            failed=1
          fi
        fi
      done
      exit "$failed"
    '';
  };
}

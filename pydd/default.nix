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
        # `PYDD_PRINT_PATH=1` prints the output path alone, for a caller to compare.
        if [[ ''${PYDD_PRINT_PATH:-} == 1 ]]; then
          echo "$out"
          exit 0
        fi
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
  # mode. `cli` and `huggorm` are `builder-rpc-v0` on Nix master, and the
  # writer chooses the path from plan to derivations. `out` is released Nix,
  # and holds the projects that it supports so far (Lillecarl/pydd#2).
  # nix run --file . run -- projects.cli.pure-app
  # nix run --file . run-released -- projects.out.pure-app
  projects =
    lib.genAttrs [ "cli" "huggorm" ] (
      writer:
      import ./tests/projects {
        inherit pkgs;
        pydd = pkgs.callPackage ./lib { inherit huggorm nix writer; };
      }
    )
    // {
      out =
        lib.getAttrs
          [
            "binary-app"
            "binary-nixpkgs"
            "local-index"
            "native-app"
            "pure-app"
            "vendored-app"
          ]
          (
            import ./tests/projects {
              inherit pkgs;
              pydd = pkgs.callPackage ./lib {
                huggorm = huggormReleased;
                nix = nixReleased;
                writer = "huggorm";
                plannerProtocol = "out";
              };
            }
          );
    };

  # Run every check of `projects` in each mode, and compare the environments.
  # Every mode must give the same content-addressed path. A check needs a
  # Nix that this machine's daemon may not be, so it builds in the private
  # store of `run` or `run-released`, and no `checks` attribute can hold it.
  test-projects = pkgs.writeShellApplication {
    name = "pydd-test-projects";
    runtimeInputs = [
      run
      run-released
      nix
    ];
    text = ''
      log=''${PYDD_LOG:-''${XDG_CACHE_HOME:-$HOME/.cache}/pydd/projects}
      mkdir -p "$log"
      if (( $# > 0 )); then
        names=("$@")
      else
        read -ra names <<< "$(nix eval --raw --file . projects.cli --apply 'p: toString (builtins.attrNames p)')"
      fi
      read -ra released <<< "$(nix eval --raw --file . projects.out --apply 'p: toString (builtins.attrNames p)')"
      failed=0
      for name in "''${names[@]}"; do
        paths=()
        modes=(cli huggorm)
        if [[ " ''${released[*]} " == *" $name "* ]]; then
          modes+=(out)
        fi
        for mode in "''${modes[@]}"; do
          runner=pydd-run
          [[ $mode == out ]] && runner=pydd-run-released
          if "$runner" "projects.$mode.$name" > "$log/$name-$mode.log" 2>&1; then
            echo "pass  $name ($mode)"
            paths+=("$mode=$(PYDD_PRINT_PATH=1 "$runner" "projects.$mode.$name" environment 2>> "$log/$name-$mode.log")")
          else
            echo "FAIL  $name ($mode)  ($log/$name-$mode.log)"
            failed=1
          fi
        done
        if (( ''${#paths[@]} == ''${#modes[@]} )); then
          if [[ $(printf '%s\n' "''${paths[@]#*=}" | sort -u | wc -l) == 1 ]]; then
            echo "same  $name  ''${paths[0]#*=}  (''${modes[*]})"
          else
            echo "DIFFER  $name  ''${paths[*]}"
            failed=1
          fi
        fi
      done
      exit "$failed"
    '';
  };
}

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

  # Writes derivations from a build, as JSON, for a planner in any language
  # (Lillecarl/pydd#1). One per Nix: only master has `builder-rpc-v0`.
  pydd-drv = pkgs.callPackage ./tools/pydd-drv { inherit (nix.libs) nix-store; };
  pydd-drv-released = pkgs.callPackage ./tools/pydd-drv { inherit (nixReleased.libs) nix-store; };

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
    else if name == "graph" then
      [
        "drv"
        "released"
        "recursive"
      ]
    else
      [
        "cli"
        "huggorm"
        "drv"
      ];
in
rec {
  inherit
    pkgs
    pydd
    huggorm
    pydd-drv
    pydd-drv-released
    ;

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
          else if writer == "released" || writer == "recursive" then
            {
              writer = "drv";
              huggorm = huggormReleased;
              nix = nixReleased;
              pyddDrv = pydd-drv-released;
              plannerProtocol = if writer == "recursive" then "recursive" else "out";
            }
          else
            {
              inherit huggorm nix writer;
              pyddDrv = pydd-drv;
            }
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
        export NIX_CONFIG="extra-experimental-features = nix-command ca-derivations dynamic-derivations recursive-nix
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

  # The planner of each project's environment. With PYDD_MEASURE set and
  # `--impure`, every planner carries it (`mkPlanner`), so Nix runs each one
  # again and every node stays cached. `--rebuild` refuses a
  # content-addressed planner. `tools/measure-modes.py` sets it.
  measure = lib.mapAttrs (_: lib.mapAttrs (_: project: project.out.planner.planner)) projects;

  # The workflow as a value, from ci/workflows/ci.nix, and as the file GitHub
  # reads. `ghalib.toYamlScript` writes a multi-line `run:` as a literal
  # block and quotes `'on'`; pynixd's default.nix gives the reasons.
  ghalib = import sources.ghanix { inherit lib; };
  ciWorkflow = import ./ci/workflows/ci.nix {
    inherit lib ghalib;
    nixVersion = pkgs.nix.version;
  };
  ciWorkflowFile =
    pkgs.runCommand "ci.yml"
      {
        nativeBuildInputs = [ (pkgs.python3.withPackages (ps: [ ps.pyyaml ])) ];
        value = builtins.toJSON ciWorkflow;
        passAsFile = [ "value" ];
      }
      ''
        {
          echo "# GENERATED FILE -- do not edit by hand."
          echo "# Edit ci/workflows/ci.nix, then run: nix run --file . ci-workflow-update"
          python3 ${ghalib.toYamlScript} "$valuePath"
        } > $out
      '';
  ci-workflow-update = pkgs.writeShellScriptBin "ci-workflow-update" ''
    cp --no-preserve=mode ${ciWorkflowFile} .github/workflows/ci.yml
  '';

  # The site that GitHub Pages serves: docs/, with Sphinx.
  pydd-docs = pkgs.python3Packages.callPackage ./nix/docs.nix { inherit (pkgs) graphviz; };

  # A derivation that does nothing, with the same nonce: the time Nix needs to
  # build anything at all, which every planner time holds.
  measure-floor = derivation {
    name = "floor";
    inherit (pkgs.stdenv.hostPlatform) system;
    builder = "${pkgs.bash}/bin/bash";
    args = [
      "-c"
      "echo > $out"
    ];
    PYDD_MEASURE = builtins.getEnv "PYDD_MEASURE";
  };

  # Writes docs/_generated, which docs/modes.md reads. Run it after
  # `test-projects`, so only the planners run.
  measure-modes = pkgs.writeShellApplication {
    name = "pydd-measure-modes";
    runtimeInputs = [
      nix
      pkgs.python3
      pkgs.jujutsu
    ];
    text = ''
      export NIX_MASTER=${lib.getExe nix}
      export NIX_RELEASED=${lib.getExe nixReleased}
      export NIX_RELEASED_VERSION=${nixReleased.version}
      exec python3 ${./tools/measure-modes.py} "$@"
    '';
  };

  # A stock release, with recursive-nix: a planner gets a daemon socket.
  # nix run --file . run-recursive -- examples.graph.recursive
  run-recursive = mkRun {
    name = "pydd-run-recursive";
    nix = nixReleased;
    store = "store-${nixReleased.version}";
    features = "recursive-nix";
  };

  checks = {
    # Does .github/workflows/ci.yml still say what ci/workflows/ci.nix says?
    # Parsed, so key order and quoting cannot fail it.
    ciWorkflow =
      pkgs.runCommand "ci-workflow-check"
        {
          nativeBuildInputs = [
            pkgs.yq-go
            pkgs.jq
          ];
        }
        ''
          yq --output-format=json '.' ${./.github/workflows/ci.yml} | jq --sort-keys . > committed.json
          jq --sort-keys . < ${pkgs.writeText "ci-rendered.json" (builtins.toJSON ciWorkflow)} > rendered.json
          if ! diff --unified committed.json rendered.json; then
            echo ".github/workflows/ci.yml and ci/workflows/ci.nix disagree." >&2
            echo "Run: nix run --file . ci-workflow-update" >&2
            exit 1
          fi
          touch $out
        '';
    tests = pkgs.callPackage ./nix/tests.nix { inherit huggorm pydd-drv; };
  };

  # The uv projects of the ddrn lab, each with its check, once for each
  # mode. `cli` and `huggorm` are `builder-rpc-v0` on Nix master, and the
  # writer chooses the path from plan to derivations. `out` is stock Nix
  # 2.35, where the host makes every node that a planner names but its root.
  # nix run --file . run -- projects.cli.pure-app
  # nix run --file . run-released -- projects.out.pure-app
  projects =
    lib.genAttrs [ "cli" "huggorm" "drv" ] (
      writer:
      import ./tests/projects {
        inherit pkgs;
        pydd = pkgs.callPackage ./lib {
          inherit huggorm nix writer;
          pyddDrv = pydd-drv;
        };
      }
    )
    // {
      out = (
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
      # recursive-nix with EnsurePath used: a stock `nix eval` evaluates
      # graph.nix in the planner, with `builtins.storePath` and
      # `builtins.appendContext`. No huggorm, and no renderer.
      recursive-eval = (
        import ./tests/projects {
          inherit pkgs;
          pydd = pkgs.callPackage ./lib {
            huggorm = huggormReleased;
            nix = nixReleased;
            writer = "cli";
            plannerProtocol = "recursive";
            plannerRender = "nix-cli";
          };
        }
      );
      # recursive-eval, with nixpkgs in the planner: a source build is a
      # `stdenv.mkDerivation`. Its environments differ from every other mode's.
      recursive-nixpkgs = (
        import ./tests/projects {
          inherit pkgs;
          pydd = pkgs.callPackage ./lib {
            huggorm = huggormReleased;
            nix = nixReleased;
            writer = "cli";
            plannerProtocol = "recursive";
            plannerRender = "nix-cli";
            plannerNixpkgs = pkgs.path;
          };
        }
      );
      # Stock 2.35 with recursive-nix: the planner writes through the build's
      # daemon socket, with the release's own `nix`, and its root to `$out`.
      recursive = (
        import ./tests/projects {
          inherit pkgs;
          pydd = pkgs.callPackage ./lib {
            huggorm = huggormReleased;
            nix = nixReleased;
            writer = "cli";
            plannerProtocol = "recursive";
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
      run-recursive
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
        modes=(cli huggorm drv recursive recursive-eval)
        if [[ " ''${released[*]} " == *" $name "* ]]; then
          modes+=(out)
        fi
        for mode in "''${modes[@]}"; do
          runner=pydd-run
          [[ $mode == out ]] && runner=pydd-run-released
          [[ $mode == recursive* ]] && runner=pydd-run-recursive
          if "$runner" "projects.$mode.$name" > "$log/$name-$mode.log" 2>&1; then
            echo "pass  $name ($mode)"
            paths+=("$mode=$(PYDD_PRINT_PATH=1 "$runner" "projects.$mode.$name" environment 2>> "$log/$name-$mode.log")")
          else
            echo "FAIL  $name ($mode)  ($log/$name-$mode.log)"
            failed=1
          fi
        done
        # stdenv builds a source node of recursive-nixpkgs, so its environment
        # differs by design. It must pass its check, and joins no comparison.
        if "pydd-run-recursive" "projects.recursive-nixpkgs.$name" > "$log/$name-recursive-nixpkgs.log" 2>&1; then
          echo "pass  $name (recursive-nixpkgs, not compared)"
        else
          echo "FAIL  $name (recursive-nixpkgs)  ($log/$name-recursive-nixpkgs.log)"
          failed=1
        fi
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

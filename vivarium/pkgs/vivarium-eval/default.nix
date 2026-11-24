# The `vivarium` CLI, built on huggorm: run by name, phases, switch, ctl,
# monitor, and `vivarium-mcp`. The package keeps the name `vivarium-eval`.
#
# On nanopynix's Python set rather than nixpkgs' python3Packages,
# because huggorm is built there and one interpreter has to hold it,
# `vivarium` and `vivarium_runner` together. `vivarium` and
# `vivarium-runner` are lifted into that set from their own
# pyproject.toml, as easykubenix does for `ekn`. The set moves out of
# nanopynix with the rest of its packaging (nixidae/nixidae#866).
#
# Nothing a test needs depends on this. `mkTest` and every check use the
# nixpkgs build of the runner, `vivarium-runner`, so `nix build` and
# `nix run` of a test never build huggorm -- the sandboxed path must not
# evaluate anyway. The dev shells that run tests carry this CLI.
{
  pkgs,
  sources,
}:
let
  inherit (pkgs) lib;

  # The umbrella's sources, handed on, as easykubenix does: from a store
  # path nanopynix would otherwise find no umbrella and fetch its own.
  nanopynix = import sources.nanopynix { inherit pkgs sources; };
  inherit ((import sources.nixvenv { inherit pkgs sources; }).lib) ps;

  projects = {
    vivarium-agent = ../vivarium-agent;
    vivarium-runner = ../vivarium-runner;
    vivarium-ca = ../vivarium-ca;
    vivarium = ../vivarium;
    vivarium-eval = ./.;
  };

  pythonSet = nanopynix.lib.pythonSetWith {
    projectRoots = lib.attrValues projects;
    overlay =
      pySelf: _pyPrev:
      lib.mapAttrs (
        _: projectRoot:
        pySelf.callPackage (ps.mkProject {
          inherit projectRoot;
          inherit (nanopynix.lib.pythonSet) python;
        }) { }
      ) projects;
  };

  app = nanopynix.lib.mkApp {
    name = "vivarium-eval";
    inherit pythonSet;
  };

  # A Python with `vivarium`, the pytest plugin and pytest: what a dev
  # shell puts on PATH to run `pytest` against guests (nixidae/nixidae#1413).
  env = pythonSet.mkVirtualEnv "vivarium-env" { vivarium-eval = [ ]; };

  # pyproject-nix builds a wheel and runs nothing, so the tests and the
  # type check are run here, in the same environment the app gets.
  tests =
    pkgs.runCommand "vivarium-eval-tests"
      {
        nativeBuildInputs = [
          env
          pkgs.zuban
        ];
      }
      ''
        cp -r --no-preserve=mode ${
          lib.fileset.toSource {
            root = ./.;
            fileset = lib.fileset.unions [
              (lib.fileset.fileFilter (file: file.hasExt "py") ./tests)
              (lib.fileset.fileFilter (file: file.hasExt "py") ./vivarium_eval)
              ./mypy.ini
            ];
          }
        }/. .
        export HOME=$TMPDIR
        zuban mypy --python-executable ${env}/bin/python
        python -m pytest -p no:cacheprovider tests
        touch $out
      '';
in
app.overrideAttrs (old: {
  meta = (old.meta or { }) // {
    mainProgram = "vivarium";
  };
  passthru = (old.passthru or { }) // {
    inherit env tests pythonSet;
  };
})

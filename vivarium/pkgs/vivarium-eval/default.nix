# `vivarium-eval`: the evaluating front door, built on huggorm.
#
# On nanopynix's Python set rather than nixpkgs' python3Packages,
# because huggorm is built there and one interpreter has to hold it,
# `vivarium` and `vivarium_runner` together. `vivarium` and
# `vivarium-runner` are lifted into that set from their own
# pyproject.toml, as easykubenix does for `ekn`. The set moves out of
# nanopynix with the rest of its packaging (nixidae/nixidae#866).
#
# Nothing else in this repository depends on this. `mkTest` and every
# check use the nixpkgs build of `vivarium`, so a consumer never
# builds huggorm -- the sandboxed path must not evaluate anyway.
{
  pkgs,
  sources,
}:
let
  inherit (pkgs) lib;

  # The umbrella's sources, handed on, as easykubenix does: from a store
  # path nanopynix would otherwise find no umbrella and fetch its own.
  nanopynix = import sources.nanopynix { inherit pkgs sources; };
  inherit (import sources.nixvenv { inherit pkgs sources; }) ps;

  projects = {
    vivarium-runner = ../vivarium-runner;
    vivarium = ../vivarium;
    vivarium-eval = ./.;
  };

  pythonSet = nanopynix.pythonSetWith {
    projectRoots = lib.attrValues projects;
    overlay =
      pySelf: _pyPrev:
      lib.mapAttrs (
        _: projectRoot:
        pySelf.callPackage (ps.mkProject {
          inherit projectRoot;
          inherit (nanopynix.pythonSet) python;
        }) { }
      ) projects;
  };

  app = nanopynix.mkApp {
    name = "vivarium-eval";
    inherit pythonSet;
  };

  # pyproject-nix builds a wheel and runs nothing, so the tests and the
  # type check are run here, in the same environment the app gets.
  tests =
    let
      python = pythonSet.mkVirtualEnv "vivarium-eval-test-env" { vivarium-eval = [ ]; };
    in
    pkgs.runCommand "vivarium-eval-tests"
      {
        nativeBuildInputs = [
          python
          pkgs.zuban
        ];
      }
      ''
        cp -r ${./tests} tests
        cp -r ${./vivarium_eval} vivarium_eval
        cp ${./mypy.ini} mypy.ini
        export HOME=$TMPDIR
        zuban mypy --python-executable ${python}/bin/python
        python -m pytest -p no:cacheprovider tests
        touch $out
      '';
in
app.overrideAttrs (old: {
  passthru = (old.passthru or { }) // {
    inherit tests pythonSet;
  };
})

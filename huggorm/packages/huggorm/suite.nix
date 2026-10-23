/**
  huggorm's suite against the built package, in a nixvenv: lint, types and
  the hermetic tests.

  Its own derivation, not the package's check phase. There the package is
  not installed anywhere yet, so only PYTHONPATH reaches it, and a tool that
  reads PYTHONPATH sees a different world from the interpreter: zuban took a
  `typing_extensions.py` there for its stub (nixidae/nixidae#1046).

  A sanitizer lane runs the tests alone. Under a preloaded TSAN runtime the
  uninstrumented ruff reports races in itself and exits 66, and the base
  lane runs ruff and zuban anyway.
*/
{
  lib,
  runCommand,
  python3,
  nixvenv,
  ruff,
  zuban,
  gitMinimal,
  huggorm,
  huggorm-bindings,
  huggorm-generated,
  huggorm-gen,
  huggorm-decl,
  huggorm-dsl,
  nix-tcp-store,
  # The CLI of the Nix the bindings link, which `private_daemon` runs.
  nix,
  # The suite branches on the `NIX_2_3x` names, and a type checker holds
  # a name constant only when told to.
  checkerFlags,
  sanitizer,
  ruffConfig,
}:
let
  venv = nixvenv {
    python = python3;
    packages = ps: [
      huggorm
      huggorm-bindings
      huggorm-generated
      # Several tests hold an artifact against the model the generator
      # builds (`declared_model()`), so the suite imports it.
      huggorm-gen
      huggorm-decl
      huggorm-dsl
      ps.pytest
      ps.anyio
      ps.pytest-timeout
    ];
    executables = [ "pytest" ];
  };
  python = "${venv}/lib/venv/bin/python";
  src = lib.fileset.toSource {
    root = ./.;
    fileset = lib.fileset.unions [
      ./pyproject.toml
      ./huggorm
      ./tests
    ];
  };
in
runCommand "huggorm-suite-${huggorm-bindings.nixVersion}"
  {
    nativeBuildInputs = [
      # The suite builds a git work tree for the git fetcher to read.
      gitMinimal
    ]
    ++ lib.optionals (sanitizer == null) [
      ruff
      zuban
    ];
    env = {
      HUGGORM_NIX_VERSION = huggorm-bindings.nixVersion;
      HUGGORM_NIX_GC = if huggorm-bindings.hasCollector then "1" else "0";
      # The plugin `test_plugins.py` loads: built against this lane's Nix,
      # as a plugin must be.
      HUGGORM_TCP_STORE_PLUGINS = "${nix-tcp-store}/lib/nix/plugins";
      HUGGORM_ORACLE_NIX = "${nix}/bin/nix";
    };
  }
  (
    ''
      cp -r --no-preserve=mode ${src} source
      cd source
      export HOME=$TMPDIR
      # The front door is emitted at build time; the tree keeps none of it.
      cp ${huggorm}/${python3.sitePackages}/huggorm/__init__.py huggorm/__init__.py
    ''
    + lib.optionalString (sanitizer == null) ''
      echo "--- lint ---"
      ruff check --no-cache --config ${ruffConfig} .
      echo "--- typecheck ---"
      # --python-executable: a PEP 561 <pkg>-stubs package is found only
      # through an interpreter, and without it every binding type reads as
      # Any and the check passes while proving nothing (nixidae/nixidae#564).
      zuban mypy --strict --python-executable ${python} ${checkerFlags} huggorm tests
      # The emitted package and the stubs, checked as a pair: the claim
      # nixidae/nixidae#554 and nixidae/nixidae#564 make, that a consumer can
      # be typechecked against the generated protocols.
      zuban mypy --strict --python-executable ${python} \
        ${huggorm-generated}/${python3.sitePackages}/huggorm_generated
    ''
    + lib.optionalString (sanitizer != null) sanitizer.testEnv
    + ''
      echo "--- pytest (hermetic only) ---"
      # -m "not live": a build sandbox has no daemon, no db and no writable
      # store. The live half runs from the devshell: nix run --file . test
      # (nixidae/nixidae#574). The `pytest` script, not `python -m`, so the
      # tree's `huggorm/` stays off sys.path and the installed one is tested.
      ${venv}/bin/pytest -p no:cacheprovider -m "not live"
      touch $out
    ''
  )

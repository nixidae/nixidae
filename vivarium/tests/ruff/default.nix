# ruff over vivarium's Python, with the anyio ban list. `ruff check --fix`
# and `ruff format` apply what it asks.
{ hostPkgs, lib, ... }:
let
  source = lib.fileset.toSource {
    root = ../..;
    fileset = lib.fileset.unions [
      (lib.fileset.fileFilter (file: file.hasExt "py" || file.name == "ruff.toml") ../..)
      ../../nix/shared/ruff-anyio.toml
    ];
  };
in
hostPkgs.runCommand "vivarium-check-ruff" { nativeBuildInputs = [ hostPkgs.ruff ]; } ''
  cd ${source}
  status=0
  ruff check --no-cache . || status=1
  ruff format --check --no-cache . || status=1
  touch $out
  exit $status
''

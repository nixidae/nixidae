{
  # Where every dependency lives. nix/sources.nix says how it finds them.
  sources ? import ./nix/sources.nix,
  system ? builtins.currentSystem,
  pkgs ? import sources.nixpkgs {
    inherit system;
    config = { };
    overlays = [ ];
  },
  # The Nix that modiff evaluates with: a nanopynix lane, such as
  # `determinate`.
  lane ? "stable",
}:
let
  # nanopynix's set carries huggorm, so modiff links the Nix it does.
  nanopynix = (import sources.nanopynix { inherit pkgs sources system; }).lanes.${lane};
  inherit ((import sources.nixvenv { inherit pkgs sources; }).lib) ps;

  # What the wheel build reads, and nothing else.
  projectRoot = pkgs.lib.fileset.toSource {
    root = ./modiff;
    fileset = pkgs.lib.fileset.unions [
      ./modiff/pyproject.toml
      ./modiff/src
    ];
  };

  overlay = pySelf: _pyPrev: {
    modiff = pySelf.callPackage (ps.mkProject {
      inherit projectRoot;
      inherit (pySelf) python;
    }) { };
  };

  pythonSet = nanopynix.pythonSetWith {
    projectRoots = [ projectRoot ];
    inherit overlay;
  };

  # The tests read the working tree through `pythonpath` in pytest.ini.
  checkEnv = pythonSet.mkVirtualEnv "modiff-check-env" {
    modiff = [ "test" ];
  };

  # The dev shell installs modiff editable, from the checkout: an edit
  # under src/ is live with no rebuild. Its build reads only the metadata,
  # so the env does not change when the code does.
  editableRoot = pkgs.lib.fileset.toSource {
    root = ./modiff;
    fileset = pkgs.lib.fileset.unions [
      ./modiff/pyproject.toml
      ./modiff/src/modiff/__init__.py
    ];
  };
  editableSet = nanopynix.pythonSetWith {
    projectRoots = [ editableRoot ];
    overlay = pySelf: _pyPrev: {
      modiff = pySelf.callPackage (ps.mkEditableProject {
        projectRoot = editableRoot;
        root = toString ./modiff;
        inherit (pySelf) python;
      }) { };
    };
  };
  devEnv = editableSet.mkVirtualEnv "modiff-dev-env" {
    modiff = [ "test" ];
  };
in
{
  packages = {
    inherit (pythonSet) modiff;
    # `nix run` reads `mainProgram`; without it, it runs `bin/modiff-env`.
    default = pkgs.lib.addMetaAttrs { mainProgram = "modiff"; } (
      pythonSet.mkVirtualEnv "modiff-env" { modiff = [ ]; }
    );
  };
  lib = { inherit projectRoot overlay; };
  checks = pkgs.callPackages ./nix/checks.nix {
    devEnv = checkEnv;
    nixpkgsPath = pkgs.path;
  };
  shell = pkgs.mkShell {
    packages = [
      devEnv
      pkgs.ruff
      pkgs.zuban
    ];
    # The tests and the default `--nixpkgs` read `<nixpkgs>`: the pinned one.
    NIX_PATH = "nixpkgs=${pkgs.path}";
  };
}

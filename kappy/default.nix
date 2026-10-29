{
  # Where every dependency lives. nix/sources.nix says how it finds them.
  sources ? import ./nix/sources.nix,
  system ? builtins.currentSystem,
  pkgs ? import sources.nixpkgs {
    inherit system;
    config = { };
    overlays = [ ];
  },
}:
let
  # nanopynix resolves `kr8s` to the fork this tree builds against.
  nanopynix = import sources.nanopynix { inherit pkgs sources system; };
  inherit ((import sources.nixvenv { inherit pkgs sources; }).lib) ps;

  /*
    The `kappy/` project directory, copied alone. A set leaves out of the
    nixpkgs lookup every root whose directory name is a declared dependency,
    so the copy keeps the name `kappy`. A copy of the whole source would
    change with `flake.lock`, which moves with every lock commit.
  */
  projectRoot =
    let
      root = toString ./.;
    in
    builtins.path {
      path = ./.;
      name = "source";
      filter =
        path: _type: path == root || path == "${root}/kappy" || pkgs.lib.hasPrefix "${root}/kappy/" path;
    }
    + "/kappy";

  # Adds `kappy` to a pyproject set whose `projectRoots` holds `projectRoot`.
  overlay = pySelf: _pyPrev: {
    kappy = pySelf.callPackage (ps.mkProject {
      inherit projectRoot;
      inherit (pySelf) python;
      extra = rendered: {
        meta = rendered.meta // {
          platforms = pkgs.lib.platforms.unix;
        };
      };
    }) { };
  };

  pythonSet = nanopynix.lib.pythonSetWith {
    projectRoots = [ projectRoot ];
    inherit overlay;
  };

  # The environment the tests and the shell run in: kappy's closure and its
  # `test` extra. The tests read the working tree through `pythonpath` in
  # pytest.ini, so the installed copy is never the one under test.
  devEnv = pythonSet.mkVirtualEnv "kappy-dev-env" {
    kappy = [ "test" ];
  };
in
{
  packages = {
    inherit (pythonSet) kappy;
    default = pythonSet.kappy;
  };
  # For a consumer's own pyproject set:
  #   pythonSetWith { projectRoots = [ kappy.lib.projectRoot ]; overlay = kappy.lib.overlay; }
  lib = { inherit projectRoot overlay; };
  nixosModules = { };
  easykubenixModules = { };
  # The tests run in `shell`; see ci/workflows/tests.nix.
  checks = { };
  docs = { };
  shell = pkgs.mkShell {
    packages = [
      devEnv
      pkgs.ruff
      pkgs.zuban
    ];
  };
  ciTestWorkflows.tests = import ./ci/workflows/tests.nix;

  # Not public: the names may change without notice.
  internal = { inherit pkgs pythonSet devEnv; };
}

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

  pythonSet = nanopynix.lib.pythonSetWith {
    projectRoots = [ ./kappy ];
    overlay = pySelf: _pyPrev: {
      kappy = pySelf.callPackage (ps.mkProject {
        projectRoot = ./kappy;
        inherit (nanopynix.lib.pythonSet) python;
        extra = rendered: {
          meta = rendered.meta // {
            platforms = pkgs.lib.platforms.unix;
          };
        };
      }) { };
    };
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
  lib = { };
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

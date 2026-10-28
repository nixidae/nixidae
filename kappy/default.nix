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
  inherit (import sources.nixvenv { inherit pkgs sources; }) ps;

  pythonSet = nanopynix.pythonSetWith {
    projectRoots = [ ./kappy ];
    overlay = pySelf: _pyPrev: {
      kappy = pySelf.callPackage (ps.mkProject {
        projectRoot = ./kappy;
        inherit (nanopynix.pythonSet) python;
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
  inherit pkgs pythonSet devEnv;
  kappy = pythonSet.kappy;
  shell = pkgs.mkShell {
    packages = [
      devEnv
      pkgs.ruff
      pkgs.zuban
    ];
  };
  ciTestWorkflows.tests = import ./ci/workflows/tests.nix;
}

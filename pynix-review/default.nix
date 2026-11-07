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
  # nanopynix's set carries huggorm, so pynix-review links the Nix it does.
  nanopynix = import sources.nanopynix { inherit pkgs sources system; };
  inherit ((import sources.nixvenv { inherit pkgs sources; }).lib) ps nixvenv;

  /*
    The `pynix-review/` project directory, copied alone. A copy of the whole
    source would change with `flake.lock`, which moves with every lock commit.
  */
  projectRoot =
    let
      root = toString ./.;
    in
    builtins.path {
      path = ./.;
      name = "source";
      filter =
        path: _type:
        path == root || path == "${root}/pynix-review" || pkgs.lib.hasPrefix "${root}/pynix-review/" path;
    }
    + "/pynix-review";

  # Adds `pynix-review` to a pyproject set whose `projectRoots` holds `projectRoot`.
  overlay = pySelf: _pyPrev: {
    pynix-review = pySelf.callPackage (ps.mkProject {
      inherit projectRoot;
      inherit (pySelf) python;
      extra = rendered: {
        meta = rendered.meta // {
          platforms = pkgs.lib.platforms.linux;
        };
      };
    }) { };
  };

  pythonSet = nanopynix.lib.pythonSetWith {
    projectRoots = [ projectRoot ];
    inherit overlay;
  };

  # The environment the tests and the shell run in. The tests read the
  # working tree through `pythonpath` in pytest.ini.
  devEnv = pythonSet.mkVirtualEnv "pynix-review-dev-env" {
    pynix-review = [ "test" ];
  };
in
{
  packages = {
    inherit (pythonSet) pynix-review;
    default = pythonSet.pynix-review;
  };
  # For a consumer's own pyproject set:
  #   pythonSetWith { projectRoots = [ pynix-review.lib.projectRoot ]; overlay = pynix-review.lib.overlay; }
  lib = { inherit projectRoot overlay; };
  nixosModules = { };
  easykubenixModules = { };
  # The tests run in `shell`; see ci/workflows/tests.nix.
  checks = { };
  docs.default = pkgs.python3Packages.callPackage ./nix/docs.nix { inherit nixvenv; };
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

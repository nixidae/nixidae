# nixvenv -- a real Python venv from nixpkgs packages, with no PYTHONPATH.
#
# nixpkgs hands packages to an interpreter through PYTHONPATH, and a tool that
# reads that variable sees a different world from the interpreter: zuban took a
# `typing_extensions.py` there for its own stub (nixidae/nixidae#1046). A venv
# is what Python itself means by "these packages", so every tool agrees.
#
# Usage:
#   inherit (import sources.nixvenv { inherit pkgs; }) nixvenv;
#   nixvenv { python = pkgs.python3; packages = ps: [ ps.anyio ]; }
#
# `ps` builds pyproject.nix package sets from nixpkgs (python-set.nix), and
# needs `sources` for pyproject.nix:
#   inherit (import sources.nixvenv { inherit pkgs sources; }) ps;
{
  pkgs,
  sources ? null,
}:
let
  nixvenv = pkgs.callPackage ./nixvenv.nix { };
  ps =
    if sources == null then
      throw "nixvenv: `ps` needs `sources`, for pyproject.nix"
    else
      pkgs.callPackage ./python-set.nix {
        pyproject-nix = import sources.pyproject-nix { inherit (pkgs) lib; };
      };
in
{
  packages = { };
  lib = { inherit nixvenv ps; };
  nixosModules = { };
  checks = pkgs.callPackages ./checks.nix { inherit nixvenv; };
  docs = { };
  ciTestWorkflows.tests = import ./ci/workflows/tests.nix;

  # Most sources, croshome and solid-kubernetes read these. nixidae/nixidae#1103.
  inherit nixvenv ps;
}

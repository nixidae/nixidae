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
{ pkgs }:
let
  nixvenv = pkgs.callPackage ./nixvenv.nix { };
in
{
  inherit nixvenv;
  checks = pkgs.callPackages ./checks.nix { inherit nixvenv; };
  ciTestWorkflows.tests = import ./ci/workflows/tests.nix;
}

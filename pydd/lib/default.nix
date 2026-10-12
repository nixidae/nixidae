/**
  The pydd library, with `writer` as the default for every planner, so an
  example is written once for both writers.
*/
{
  callPackage,
  huggorm,
  nix,
  writer ? "cli",
}:
let
  planner = callPackage ./planner.nix { };
  self = planner // {
    mkPlanner = args: planner.mkPlanner ({ inherit writer huggorm nix; } // args);
    python = callPackage ./python.nix { pydd = self; };
    evaluate = callPackage ./evaluate.nix { pydd = self; };
  };
in
self

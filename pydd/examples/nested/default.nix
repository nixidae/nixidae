# A planner whose graph holds a second planner. The root reads the output of
# the graph that the inner planner submits.
{
  pydd,
  bash,
  stdenv,
}:
let
  inner =
    (pydd.mkPlanner {
      name = "hello";
      writer = "cli";
      plan = ../hello/plan.py;
      menu.bash = "${bash}";
    }).planner;
in
pydd.evaluate.mkEvaluated {
  name = "nested";
  expression = ./graph.nix;
  args = {
    inherit (stdenv.hostPlatform) system;
    bash = "${bash}";
    # A `.drv` that a planner writes is absent from its own sandbox, and
    # `outputOf` reads it. Made here, it is an input of the planner.
    inner = pydd.candidate inner { };
  };
}

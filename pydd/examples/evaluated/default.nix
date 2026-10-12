# The graph as a Nix file, evaluated in the planner. Two candidates are
# offered, and the expression reads one: the other is never built.
{
  pydd,
  bash,
  stdenv,
  writeText,
}:
pydd.evaluate.mkEvaluated {
  name = "evaluated";
  expression = ./graph.nix;
  args = {
    inherit (stdenv.hostPlatform) system;
    bash = "${bash}";
    candidates = {
      greeting = pydd.candidate (writeText "greeting" "hello from a candidate\n") { };
      unread = pydd.candidate (writeText "unread" "nobody reads this\n") { };
    };
  };
}

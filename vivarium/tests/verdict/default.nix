/*
  Does a red run fail its check? The negative control for nix/verdict.py:
  an attempt never fails, so a reader that lost the verdict would turn
  every red run green.

  - A phase that fails.
  - A runner killed before it writes `status`: the attempt writes 1.
  - A `pytestCheck` with a failing test.

  Each must fail its build with exit 1 and say so in the log.
*/
{
  hostPkgs,
  mkTest,
  vivarium,
  ...
}:
let
  red =
    name: script:
    mkTest {
      inherit name;
      nodes.one = { };
      phases.red.script = script;
    };
  fails =
    drv: entries:
    hostPkgs.testers.testBuildFailure' {
      inherit drv;
      expectedBuilderLogEntries = entries;
    };
in
hostPkgs.linkFarm "vivarium-check-verdict" {
  phase = fails (red "verdict-phase" ../phases/cluster.py) [ "the run failed (exit 1)" ];
  killed = fails (red "verdict-killed" ../phases/die.py) [ "the run failed (exit 1)" ];
  pytest = fails (vivarium.pytestCheck {
    name = "verdict";
    src = ./red;
    runs = { };
  }) [ "the run failed (exit 1)" ];
}

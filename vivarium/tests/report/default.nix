/*
  Does `report.json` say where the time went before the first phase and
  after the last? Two guests, so the second one launches after the first.
  See tests/checks/report.py.
*/
{
  pkgs,
  mkTest,
  checkScript,
  ...
}:
let
  run = mkTest {
    name = "report";
    nodes.a = { };
    nodes.b = { };
    phases.hello = {
      script = ../phases/report.py;
      after = [ "boot" ];
    };
  };
in
pkgs.runCommand "vivarium-check-report" { passthru.session = run; } ''
  ${pkgs.python3.interpreter} ${checkScript "report.py"} ${run.attempt}
  touch $out
''

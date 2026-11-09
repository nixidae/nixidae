/*
  Can a person reach into a paused run?

  The loop this is for: changing a phase costs an evaluation and a
  store path, and changing a guest costs an image, so the fast way is
  neither -- pause with the guests up and send Python in. And can
  `ctl stop` end the run? tests/checks/breakpoint.py.
*/
{
  pkgs,
  lib,
  mkTest,
  cli,
  ...
}:
let
  run = mkTest {
    name = "breakpoint";
    nodes.one = { };
    phases.later = {
      script = ../phases/independent.py;
      after = [ "boot" ];
    };
  };
in
pkgs.runCommand "vivarium-check-breakpoint" { passthru.session = run; } ''
  ${pkgs.python3.interpreter} ${../checks}/breakpoint.py ${lib.getExe cli} ${lib.getExe run.driver} "$TMPDIR"
  touch $out
''

# Can `exec` reach a run that is not paused? tests/live-exec.py.
{
  pkgs,
  lib,
  mkTest,
  cli,
  ...
}:
let
  run = mkTest {
    name = "live-exec";
    nodes.one = { };
    phases.live = {
      script = ../phases/live.py;
      after = [ "boot" ];
    };
  };
in
pkgs.runCommand "vivarium-check-live-exec" { passthru.session = run; } ''
  ${pkgs.python3.interpreter} ${../live-exec.py} ${lib.getExe cli} ${run.spec} "$TMPDIR"
  touch $out
''

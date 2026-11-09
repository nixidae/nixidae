/*
  The command lines a run prints -- monitor, exec, continue -- run as
  they are, copied from its output: an agent that knows nothing of
  vivarium can reach a run it only started. tests/copy-commands.py.
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
    name = "copy-commands";
    nodes.one = { };
    phases.later = {
      script = ../phases/independent.py;
      after = [ "boot" ];
    };
  };
in
pkgs.runCommand "vivarium-check-copy-commands" { passthru.session = run; } ''
  ${pkgs.python3.interpreter} ${../copy-commands.py} ${lib.getExe cli} ${run.spec} "$TMPDIR"
  touch $out
''

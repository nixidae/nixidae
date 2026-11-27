/*
  Does the runner refuse a run whose guest needs what the host lacks,
  before any guest boots, naming each need, its reason and its remedy?
  The guest needs a device, a path and a probe no host has, and a path
  and a probe every host has; only the first three are named.
  tests/checks/needs.py.
*/
{
  checkScript,
  pkgs,
  lib,
  mkTest,
  ...
}:
let
  run = mkTest {
    name = "needs";
    nodes.one.vivarium.needs = {
      absent-device = {
        on = "host";
        kind = "device";
        path = "/dev/vivarium-absent";
        why = "the check asks for a device no host has";
        remedy = "none: the check expects this to be missing";
      };
      absent-path = {
        on = "host";
        kind = "path";
        path = "/proc/vivarium-absent";
        why = "the check asks for a path no host has";
        remedy = "none: the check expects this to be missing";
      };
      present-path = {
        on = "host";
        kind = "path";
        path = "/proc/self";
        why = "every host has it";
        remedy = "none";
      };
      failing-probe = {
        on = "host";
        kind = "probe";
        command = [ (lib.getExe' pkgs.coreutils "false") ];
        why = "the check runs a probe that fails";
        remedy = "none: the check expects this to be missing";
      };
      passing-probe = {
        on = "host";
        kind = "probe";
        command = [ (lib.getExe' pkgs.coreutils "true") ];
        why = "the check runs a probe that passes";
        remedy = "none";
      };
    };
    phases.never = {
      script = ../kept.py;
      after = [ "boot" ];
    };
  };
in
pkgs.runCommand "vivarium-check-needs" { passthru.session = run; } ''
  ${pkgs.python3.interpreter} ${checkScript "needs.py"} ${lib.getExe run.driver} "$TMPDIR"
  touch $out
''

/*
  Does `--only` boot only what its phases need?

  `check` runs on `a`, and `a` requires `b`. `other` runs on `c`, and
  `--only check` deselects it, so `c` never boots. The `boot` recipe
  is in the `after` closure and takes the guests of `check` alone.
  tests/checks/on_demand.py.
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
    name = "on-demand";
    nodes = {
      a.vivarium.requires = [ "b" ];
      b = { };
      c = { };
    };
    phases = {
      check = {
        script = ../phases/on-demand.py;
        after = [ "boot" ];
        nodes = [ "a" ];
      };
      other = {
        script = ../phases/check.py;
        after = [ "boot" ];
        nodes = [ "c" ];
      };
    };
  };
in
pkgs.runCommand "vivarium-check-on-demand" { passthru.session = run; } ''
  ${pkgs.python3.interpreter} ${../checks}/on_demand.py ${lib.getExe cli} ${lib.getExe run.driver} "$TMPDIR"
  touch $out
''

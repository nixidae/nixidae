# Does `.driverInteractive` behave like nixos-test's? tests/interactive.py.
{
  pkgs,
  lib,
  mkTest,
  ...
}:
let
  run = mkTest {
    name = "interactive";
    nodes.one = { };
    interactive.nodes.one.environment.etc."vivarium-interactive".text = "yes\n";
    phases.hello = {
      script = ../phases/hello.py;
      after = [ "boot" ];
    };
  };
in
pkgs.runCommand "vivarium-check-interactive" { passthru.session = run; } ''
  export HOME=$TMPDIR
  ${pkgs.python3.interpreter} ${../interactive.py} ${lib.getExe run.driverInteractive} "$TMPDIR"
  touch $out
''

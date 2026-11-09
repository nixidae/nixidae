/*
  Does a failure skip what depends on it, and nothing else?

  The claim the whole phase design rests on, against a booted guest.
  Four phases: `boot` passes, `cluster` fails on purpose, `check`
  needs `cluster` and must be skipped, `independent` needs only
  `boot` and must still run.

  Both neighbours get this wrong, which is why it is worth a test.
  nixpkgs' driver re-raises out of `subtest`, so `independent` would
  never run and a real bug in it would stay invisible behind an
  unrelated failure. pytest would run `check` anyway, against a
  cluster that was never built, and report a second failure that
  says nothing.

  The session derivation *fails* when a phase fails -- which is
  correct -- so this reads `phases.json` from the attempt instead.
*/
{ pkgs, mkTest, ... }:
let
  run = mkTest {
    name = "phase-rules";
    nodes.one = { };
    phases = {
      boot.script = ../phases/boot.py;
      cluster = {
        script = ../phases/cluster.py;
        after = [ "boot" ];
      };
      check = {
        script = ../phases/check.py;
        after = [ "cluster" ];
      };
      independent = {
        script = ../phases/independent.py;
        after = [ "boot" ];
      };
    };
  };
in
pkgs.runCommand "vivarium-check-phase-rules" { passthru.session = run; } ''
  ${pkgs.python3.interpreter} ${../phase_rules.py} ${run.attempt}
  touch $out
''

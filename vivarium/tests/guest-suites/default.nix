/*
  Can a suite that has to run inside a guest report like one that
  runs on the host?

  pynixd's suites start daemons and build into stores they make, so
  they run in the guest and write JUnit there. The session reads
  `/artifacts/junit/*.xml` back at the end of each phase and each
  test becomes a case: an event with its machine and phase, and a
  case in the run's own junit.xml.

  Also the two things generating phases from one list needs: one
  script serving two phases through `vms.phase`, and a value crossing
  from one phase to the next through `vms.shared`.
*/
{ pkgs, mkTest, ... }:
let
  run = mkTest {
    name = "guest-suites";
    nodes.one = { };
    phases = {
      census = {
        script = ../phases/suite.py;
        after = [ "boot" ];
      };
      suite = {
        script = ../phases/suite.py;
        after = [ "census" ];
      };
    };
  };
in
pkgs.runCommand "vivarium-check-guest-suites" { passthru.session = run; } ''
  ${pkgs.python3.interpreter} ${../checks}/guest_suites.py ${run.attempt}
  touch $out
''

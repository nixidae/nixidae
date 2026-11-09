/*
  Is a pytest phase pytest, against real guests?

  `tests/cases` uses what a test author reaches for: a guest as a
  fixture, an async fixture with a teardown, parametrize, a skip, and
  one assertion that fails on purpose. The phase fails, so this reads
  the attempt.

  The claims: each test is a JUnit case of its own, the failure
  message is pytest's rewritten assertion, the fixture's teardown ran
  on the guest, and a journal entry and a command both name the test
  that caused them.
*/
{ pkgs, mkTest, ... }:
let
  run = mkTest {
    name = "pytest";
    nodes.one = { };
    phases.cases = {
      pytest.tests = ../cases;
      after = [ "boot" ];
    };
  };
in
pkgs.runCommand "vivarium-check-pytest-phase" { passthru.session = run; } ''
  ${pkgs.python3.interpreter} ${../checks}/pytest_phase.py ${run.attempt}
  touch $out
''

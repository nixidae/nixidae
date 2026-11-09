/*
  Does a guest's journal survive the guest?

  A unit logs a line, the phase waits until the line is in the
  host-side file while the guest still runs, and then kills the guest
  with SIGKILL -- no shutdown, nothing flushed. The line must be in
  `events.jsonl` afterwards as an event that carries the machine, the
  unit and the phase. That is the question an agent asks with `jq`,
  and the failure it has to answer for is the spectacular kind.
*/
{ pkgs, mkTest, ... }:
let
  run = mkTest {
    name = "stream";
    nodes.one = { };
    phases.crash = {
      script = ../phases/crash.py;
      after = [ "boot" ];
    };
  };
in
pkgs.runCommand "vivarium-check-stream" { passthru.session = run; } ''
  ${pkgs.python3.interpreter} ${../checks}/stream.py ${run.attempt}
  touch $out
''

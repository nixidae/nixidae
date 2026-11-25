/*
  Does a named environment keep `vivarium.persist` and `vms.state` across
  its runs, and does every other run start blank? Two runs into one
  environment directory, then one into a plain `--out`.
  tests/checks/kept.py, nixidae/nixidae#1588.
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
    name = "kept";
    nodes.one = {
      vivarium.persist = [ "/var/lib/probe" ];
      # Sparse; small, so a run in the sandbox makes ext4 quickly.
      vivarium.persistSize = 256;
    };
    phases.count = {
      script = ../kept.py;
      after = [ "boot" ];
    };
  };
in
pkgs.runCommand "vivarium-check-kept" { passthru.session = run; } ''
  ${pkgs.python3.interpreter} ${checkScript "kept.py"} ${lib.getExe run.driver} "$TMPDIR"
  touch $out
''

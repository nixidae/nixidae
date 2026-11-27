/*
  Does a named environment keep `vivarium.persist` and `vms.state` across
  its runs, and does every other run start blank? Two runs into one
  environment directory, then one into a plain `--out`.
  tests/checks/kept.py, nixidae/nixidae#1588.

  `.uml`, `.qemu`, `.krun` and `.container` run it on one backend each:
  the store is a raw disk under the first three and a host directory
  under the container backend.
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
  check =
    name: session: passthru:
    pkgs.runCommand "vivarium-check-${name}"
      {
        passthru = {
          inherit session;
        }
        // passthru;
        # The driver runs in this build, so it needs what the guests need.
        inherit (session.attempt) requiredSystemFeatures;
      }
      ''
        ${pkgs.python3.interpreter} ${checkScript "kept.py"} ${lib.getExe session.driver} "$TMPDIR"
        touch $out
      '';
in
check "kept" run (
  lib.genAttrs [ "uml" "qemu" "krun" "container" ] (
    backend: check "kept-${backend}" run.${backend} { }
  )
)

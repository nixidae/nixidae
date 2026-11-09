# Does a run leave nothing behind, however it ends? tests/cleanup.py.
# `.qemu` and `.krun` ask for KVM, as every test's variants do.
{
  pkgs,
  lib,
  mkTest,
  session,
  cli,
  ...
}:
let
  run = mkTest {
    name = "cleanup";
    nodes.one = { };
    phases.hold = {
      script = ../phases/hold.py;
      after = [ "boot" ];
    };
  };
  on =
    backend:
    let
      session = if backend == "uml" then run else run.${backend};
    in
    pkgs.runCommand "vivarium-check-cleanup-${backend}"
      {
        passthru.session = session;
        inherit (session.attempt) requiredSystemFeatures;
      }
      ''
        ${pkgs.python3.interpreter} ${../cleanup.py} ${lib.getExe cli} ${session.spec} "$TMPDIR" ${backend}
        touch $out
      '';
in
on "uml"
// {
  qemu = on "qemu";
  krun = on "krun";
}

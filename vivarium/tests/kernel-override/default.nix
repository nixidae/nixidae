/*
  Does `--kernel` boot the kernel it names?

  The by-hand loop for kernel work: `make` in a tree, then boot what it
  made, without a Nix build per change. A copy of the Nix-built kernel
  outside the store stands in for a working tree's `linux`. Both
  directions: the copy boots and the run passes, and a path that is not
  a kernel fails the boot -- so what boots is the override, not the
  spec's kernel behind it.
*/
{
  pkgs,
  lib,
  mkTest,
  ...
}:
let
  run = mkTest {
    name = "kernel";
    nodes.one = { };
  };
in
pkgs.runCommand "vivarium-check-kernel-override" { passthru.session = run; } ''
  ${pkgs.python3.interpreter} ${../checks}/kernel_override.py ${run.spec} ${lib.getExe run.driver} "$TMPDIR"
  touch $out
''

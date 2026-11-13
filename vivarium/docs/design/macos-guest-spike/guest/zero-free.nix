# Built in the guest right before a save: runs zerofree.c there.
# ZEROFREE_MIB sets how much to touch; empty means the free pages less 64 MiB.
{
  nonce ? toString builtins.currentTime,
  mib ? builtins.getEnv "ZEROFREE_MIB",
}:
let
  pkgs = import <nixpkgs> { system = "aarch64-darwin"; };
  zerofree = pkgs.runCommandCC "zerofree" { } ''
    mkdir -p $out/bin
    $CC -O2 -o $out/bin/zerofree ${./zerofree.c}
  '';
in
derivation {
  name = "zero-free-${nonce}";
  system = "aarch64-darwin";
  builder = "/bin/sh";
  args = [
    "-c"
    "${zerofree}/bin/zerofree ${mib} > $out"
  ];
}

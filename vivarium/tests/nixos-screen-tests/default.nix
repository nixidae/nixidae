/*
  nixpkgs' tests that read a screen, unchanged: `cage` is Wayland and
  sets `enableOCR`, `xterm` is X11 and is found by its script. They
  need /dev/kvm. By hand: `byHand` in ci/workflows.nix says why.
*/
{
  pkgs,
  lib,
  fromNixosTest,
  ...
}:
lib.genAttrs [
  "cage"
  "xterm"
] (name: fromNixosTest (pkgs.path + "/nixos/tests/${name}.nix"))

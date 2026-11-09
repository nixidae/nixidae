/*
  nixpkgs' tests that read a screen, unchanged: `cage` is Wayland and
  sets `enableOCR`, `xterm` is X11 and is found by its script. By
  hand: they need /dev/kvm.
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

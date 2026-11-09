/*
  nixpkgs' own nixos tests, through the mapper, unchanged. Cheap ones
  with no screen: `nixos-tests.simple-vm`, and so on.
*/
{
  pkgs,
  lib,
  fromNixosTest,
  ...
}:
lib.genAttrs [
  "simple-vm"
  "systemd-no-tainted"
  "oh-my-zsh"
] (name: fromNixosTest (pkgs.path + "/nixos/tests/${name}.nix"))

/*
  Does what a guest writes reach the host, and who is left running?

  Two guests, because each one must get its own directory.
  `pkgs.util-linux` for `mountpoint`.
*/
{
  pkgs,
  lib,
  mkTest,
  ...
}:
mkTest {
  name = "artifacts";
  phases.test = {
    script = ../artifacts.py;
    after = [ "boot" ];
  };
  nodes = lib.genAttrs [ "one" "two" ] (_: {
    environment.systemPackages = [ pkgs.util-linux ];
    users.users.tester.isNormalUser = true;
  });
}

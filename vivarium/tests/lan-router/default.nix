# `lan` with both guests behind the run's router (nixidae/nixidae#1404).
{ mkTest, pair, ... }:
mkTest {
  name = "lan-router";
  router.enable = true;
  phases.test = {
    script = ../lan.py;
    after = [ "boot" ];
  };
  nodes = pair "lan";
}

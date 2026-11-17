# `lan` with each guest's uplink on vsock, with no router guest: its own
# relay in a namespace, an egress per guest on the host
# (nixidae/nixidae#1404).
{ mkTest, pair, ... }:
mkTest {
  name = "lan-vsock";
  defaults.vivarium.uplink.proxy = "vsock";
  phases.test = {
    script = ../lan.py;
    after = [ "boot" ];
  };
  nodes = pair "lan";
}

/*
  A management segment the test serves (nixidae/nixidae#958): a NixOS
  services guest runs Kea DHCP4 on a segment, and a peer joins that
  segment as its uplink (`vec0`) and leases a management address there.
  The run's router is the default management switch
  (nixidae/nixidae#1523); `uplink.segment` lets a test bring its own.
*/
{ mkTest, servicesNode, ... }:
mkTest {
  name = "oob";
  backend = "qemu";
  nodes.svc = servicesNode { segment = "mgmt"; };
  nodes.client.vivarium.uplink.segment = "mgmt";
  phases.oob = {
    script = ../phases/oob.py;
    after = [ "boot" ];
  };
}

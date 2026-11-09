/*
  The out-of-band management fabric (nixidae/nixidae#958): a NixOS
  services guest runs Kea DHCP4 on a segment, and a peer joins that
  segment as its uplink (`vec0`, not passt) and leases a management
  address there. This is how a foreign guest's management interface
  will reach the fabric.
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

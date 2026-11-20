/*
  A management segment the test serves, under UML (nixidae/nixidae#958):
  the same lease as `oob`, but both guests are UML processes. It proves
  a UML guest's uplink joins a segment the test names: vec0 on a segment
  fd, not vsock to an egress.
*/
{ mkTest, servicesNode, ... }:
mkTest {
  name = "oob-uml";
  nodes.svc = servicesNode { segment = "mgmt"; };
  nodes.client.vivarium.uplink.segment = "mgmt";
  phases.oob = {
    script = ../phases/oob.py;
    after = [ "boot" ];
  };
}

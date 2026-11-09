/*
  The out-of-band fabric under UML (nixidae/nixidae#958): the same
  lease as `oob`, but both guests are UML processes. It proves a UML
  guest's uplink joins a management segment -- vec0 on a segment fd
  instead of the passt bridge -- which is what VyOS needs to reach the
  fabric, since VyOS runs under UML.
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

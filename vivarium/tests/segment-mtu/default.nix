/*
  `segments.<name>`: a declared segment's MTU reaches each interface on
  it, and an undeclared segment's interface keeps the guest's
  `vivarium.mtu`. Each guest has one of each, so a per-guest MTU fails.
  `narrow` is a hub: its two members' frames pass through the runner.
*/
{ mkTest, ... }:
let
  guest = n: {
    vivarium.interfaces = {
      narrow = {
        segment = "narrow";
        addresses = [ "10.81.1.${toString n}/24" ];
      };
      wide = {
        segment = "wide";
        addresses = [ "10.81.2.${toString n}/24" ];
      };
    };
  };
in
mkTest {
  name = "segment-mtu";
  segments.narrow = {
    mtu = 1400;
    hub = true;
  };
  nodes.a = guest 1;
  nodes.b = guest 2;
  phases.mtu = {
    script = ../phases/segment-mtu.py;
    after = [ "boot" ];
  };
}

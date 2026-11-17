# Does a guest launch, list and terminate guests through vivarium's
# cloud API, and see capacity refusals and an interruption notice?
# nixidae/nixidae#1408.
{ mkTest, pkgs, ... }:
mkTest {
  name = "cloud";
  segments.fleet.instanceAddresses = [ "10.9.0.100/24" ];
  nodes.controller = {
    vivarium.cloud = true;
    vivarium.interfaces.eth1 = {
      segment = "fleet";
      addresses = [ "10.9.0.1/24" ];
    };
    environment.systemPackages = [ pkgs.curl ];
  };
  templates.worker = {
    vivarium.cloud = true;
    vivarium.interfaces.eth1.segment = "fleet";
    environment.systemPackages = [ pkgs.curl ];
  };
  phases.test = {
    script = ../cloud.py;
    after = [ "boot" ];
  };
}

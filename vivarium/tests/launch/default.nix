# Does a phase boot guests from a template while the run goes, and take
# them down again? nixidae/nixidae#1408.
{ mkTest, ... }:
mkTest {
  name = "launch";
  segments.fleet.instanceAddresses = [ "10.9.0.100/24" ];
  nodes.cloud.vivarium.interfaces.eth1 = {
    segment = "fleet";
    addresses = [ "10.9.0.1/24" ];
  };
  templates.worker.vivarium.interfaces.eth1.segment = "fleet";
  phases.test = {
    script = ../launch.py;
    after = [ "boot" ];
  };
}

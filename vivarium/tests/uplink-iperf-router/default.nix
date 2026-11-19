/*
  `uplink-iperf` with the guest behind the run's router: the router's
  egress binds the forward (nixidae/nixidae#1404).
*/
{
  hostPkgs,
  lib,
  mkTest,
  ...
}:
mkTest {
  name = "uplink-iperf-router";
  backend = "krun";
  router.enable = true;
  settings.iperf3 = lib.getExe hostPkgs.iperf3;
  phases.test = {
    script = ../uplink-iperf.py;
    after = [ "boot" ];
  };
  nodes.node = {
    imports = [ ../../modules/iperf3.nix ];
    services.iperf3-server.enable = true;
    vivarium.inbound = [ { guest = 5201; } ];
  };
}

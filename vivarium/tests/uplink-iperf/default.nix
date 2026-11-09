/*
  What the uplink carries, passt against gvproxy (nixidae/nixidae#1163).

  Two krun guests in one run, so the two are measured back to back on
  the same host. The host runs the iperf3 client through each guest's
  forward, both directions.
*/
{
  pkgs,
  lib,
  mkTest,
  ...
}:
mkTest {
  name = "uplink-iperf";
  backend = "krun";
  settings.iperf3 = lib.getExe pkgs.iperf3;
  phases.test = {
    script = ../uplink-iperf.py;
    after = [ "boot" ];
  };
  nodes = lib.genAttrs [ "passt" "gvproxy" ] (proxy: {
    imports = [ ../../modules/iperf3.nix ];
    services.iperf3-server.enable = true;
    vivarium.uplink.proxy = proxy;
    vivarium.forward = [ { ports = [ 5201 ]; } ];
  });
}

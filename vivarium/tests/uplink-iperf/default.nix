/*
  What each uplink carries (nixidae/nixidae#1163, #1404).

  The krun guests run in one run, so the uplinks are measured back to
  back on the same host. The host runs the iperf3 client through each
  guest's forward, both directions. passt is Linux only.
*/
{
  hostPkgs,
  lib,
  mkTest,
  ...
}:
mkTest {
  name = "uplink-iperf";
  backend = "krun";
  # Each guest names its own uplink; a router guest would carry nothing.
  router.enable = false;
  settings.iperf3 = lib.getExe hostPkgs.iperf3;
  phases.test = {
    script = ../uplink-iperf.py;
    after = [ "boot" ];
  };
  nodes =
    lib.genAttrs
      (
        lib.optional hostPkgs.stdenv.hostPlatform.isLinux "passt"
        ++ [
          "gvproxy"
          "vsock"
        ]
      )
      (proxy: {
        imports = [ ../../modules/iperf3.nix ];
        services.iperf3-server.enable = true;
        vivarium.uplink.proxy = proxy;
        vivarium.inbound = [ { guest = 5201; } ];
      });
}

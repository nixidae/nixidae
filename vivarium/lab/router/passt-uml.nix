# The baseline for E4: a UML guest's own passt uplink, measured with the
# same phase as checks.uplink-iperf.
#
#   nix run --file lab/router/passt-uml.nix driver -- --out DIR
{
  sources ? import ../../nix/sources.nix,
  pkgs ? import sources.nixpkgs { },
}:
let
  vivarium = import ../.. { inherit sources pkgs; };
in
vivarium.lib.mkTest {
  name = "passt-uml";
  backend = "uml";
  settings.iperf3 = pkgs.lib.getExe pkgs.iperf3;
  phases.test = {
    script = ../../tests/uplink-iperf.py;
    after = [ "boot" ];
  };
  nodes.passt = {
    imports = [ ../../modules/iperf3.nix ];
    services.iperf3-server.enable = true;
    vivarium.forward = [ { ports = [ 5201 ]; } ];
    vivarium.mtu = pkgs.lib.toInt (builtins.getEnv "LAB_MTU");
  };
}

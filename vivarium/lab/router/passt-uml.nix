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
  # LAB_BACKEND=krun for E2's baseline.
  backend =
    let
      env = builtins.getEnv "LAB_BACKEND";
    in
    if env == "" then "uml" else env;
  settings.iperf3 = pkgs.lib.getExe pkgs.iperf3;
  phases.test = {
    script = ../../tests/uplink-iperf.py;
    after = [ "boot" ];
  };
  nodes.passt = {
    imports = [ ../../modules/iperf3.nix ];
    services.iperf3-server.enable = true;
    vivarium.inbound = [ { guest = 5201; } ];
    vivarium.mtu = pkgs.lib.toInt (builtins.getEnv "LAB_MTU");
    # A krun guest's uplink stays at 1500 whatever vivarium.mtu says
    # (nixidae/nixidae#1179). On UML this setting leaves vec0 at 1500.
    networking.interfaces = pkgs.lib.optionalAttrs (builtins.getEnv "LAB_BACKEND" == "krun") {
      vec0.mtu = pkgs.lib.toInt (builtins.getEnv "LAB_MTU");
    };
  };
}

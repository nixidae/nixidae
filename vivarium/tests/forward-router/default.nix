# `forward` with the guest behind the run's router: the router's egress
# binds the forwards, not passt (nixidae/nixidae#1404).
{ pkgs, mkTest, ... }:
mkTest {
  name = "forward-router";
  router.enable = true;
  phases.test = {
    script = ../forward.py;
    after = [ "boot" ];
  };
  nodes.node = {
    vivarium.forward = [ { ports = "all"; } ];
    environment.systemPackages = [ pkgs.python3 ];
  };
}

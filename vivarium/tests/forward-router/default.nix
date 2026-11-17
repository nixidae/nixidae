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
  nodes.node =
    { config, ... }:
    {
      # ssh, and forward.py's PLAIN and PRIVILEGED.
      vivarium.forward = [
        {
          ports = [
            config.vivarium.sshPort
            8080
            80
          ];
        }
      ];
      environment.systemPackages = [ pkgs.python3 ];
    };
}

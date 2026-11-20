# `forward` with the guest behind the run's router: the router's egress
# binds the forwards (nixidae/nixidae#1404).
{ pkgs, mkTest, ... }:
mkTest {
  name = "forward-router";
  router.enable = true;
  phases.test = {
    script = ../forward.py;
    after = [ "boot" ];
  };
  phases.runtime = {
    script = ../inbound-runtime.py;
    after = [ "test" ];
  };
  nodes.node =
    { config, ... }:
    {
      # ssh, and forward.py's PLAIN and PRIVILEGED.
      vivarium.inbound = [
        { guest = config.vivarium.sshPort; }
        { guest = 8080; }
        { guest = 80; }
      ];
      environment.systemPackages = [
        pkgs.python3
        pkgs.socat
      ];
    };
}

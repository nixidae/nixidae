/*
  Can the host reach a service in a guest?

  The only test that connects inwards.  One guest with three ports
  forwarded, and web servers started long after passt stopped accepting
  arguments.
*/
{ pkgs, mkTest, ... }:
mkTest {
  name = "forward";
  phases.test = {
    script = ../forward.py;
    after = [ "boot" ];
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
      environment.systemPackages = [ pkgs.python3 ];
    };
}

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

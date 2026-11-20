# `forward` with the guest's uplink named vsock: its own egress binds the
# forwards (nixidae/nixidae#1404).
{ pkgs, mkTest, ... }:
mkTest {
  name = "forward-vsock";
  router.enable = false;
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
      vivarium.uplink.proxy = "vsock";
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

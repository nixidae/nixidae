# `forward` with the guest's uplink on vsock: its egress binds the
# forwards, not passt (nixidae/nixidae#1404).
{ pkgs, mkTest, ... }:
mkTest {
  name = "forward-vsock";
  router.enable = false;
  phases.test = {
    script = ../forward.py;
    after = [ "boot" ];
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
      environment.systemPackages = [ pkgs.python3 ];
    };
}

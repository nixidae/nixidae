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

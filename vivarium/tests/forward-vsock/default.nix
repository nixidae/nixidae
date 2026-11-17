# `forward` with the guest's uplink on vsock: its egress binds the
# forwards, not passt (nixidae/nixidae#1404).
{ pkgs, mkTest, ... }:
mkTest {
  name = "forward-vsock";
  phases.test = {
    script = ../forward.py;
    after = [ "boot" ];
  };
  nodes.node = {
    vivarium.uplink.proxy = "vsock";
    vivarium.forward = [ { ports = "all"; } ];
    environment.systemPackages = [ pkgs.python3 ];
  };
}

/*
  The services guest as the OOB gateway (nixidae/nixidae#958): the
  single point the host reaches an out-of-band guest through. `sw`'s
  uplink is on the management segment and has no passt forward of its
  own; the host reaches a service in it through one passt forward on
  `svc`, which DNATs to `sw`. The reply only returns because Kea gives
  `sw` the default route back to `svc`, so this proves both halves at
  once.
*/
{
  pkgs,
  mkTest,
  servicesNode,
  ...
}:
mkTest {
  name = "oob-gateway";
  backend = "qemu";
  nodes.svc =
    { nodes, ... }:
    {
      imports = [
        (servicesNode {
          segment = "mgmt";
          gateway = true;
          # Pin `sw` to 10.42.0.10, outside the pool, by its uplink MAC,
          # so the DNAT target is stable. Read from the node, so it stays
          # right whatever order the nodes evaluate in.
          reservations = [
            {
              hw-address = nodes.sw.vivarium.uplinkMac;
              ip-address = "10.42.0.10";
            }
          ];
          forwardPorts = [
            {
              sourcePort = 8080;
              destination = "10.42.0.10:8080";
              proto = "tcp";
            }
          ];
        })
      ];
      # passt must forward 8080 for the DNAT to have anything to rewrite;
      # its forwards are fixed before boot.
      vivarium.forward = [ { ports = [ 8080 ]; } ];
    };
  nodes.sw = {
    vivarium.uplink.segment = "mgmt";
    environment.systemPackages = [ pkgs.python3 ];
  };
  phases.gateway = {
    script = ../phases/oob-gateway.py;
    after = [ "boot" ];
  };
}

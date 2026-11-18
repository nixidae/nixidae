/*
  VyOS behind the router (nixidae/nixidae#1523): the vyos-guest module
  DHCPs eth0 on the uplink segment, so VyOS leases its address from the
  router, and reaches the host through the router's relay.
*/
{
  vivarium,
  mkTest,
  images,
  ...
}:
mkTest {
  name = "vyos-router";
  router.enable = true;
  nodes.vyos = {
    vivarium.memory = "1024M";
    imports = [ vivarium.kinds.vyos ];
    vivarium.vyos = images.vyos-rolling // {
      config = ''
        system {
            host-name "vyos-router"
        }
      '';
    };
  };
  phases.uplink = {
    script = ../phases/uplink-eth0.py;
    after = [ "boot" ];
  };
}

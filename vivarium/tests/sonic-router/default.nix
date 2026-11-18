/*
  SONiC behind the router (nixidae/nixidae#1523): SONiC's management
  port eth0 is its uplink, and its DHCP client leases from the router;
  SONiC reaches the host through the router's relay.
*/
{
  vivarium,
  mkTest,
  images,
  ...
}:
mkTest {
  name = "sonic-router";
  backend = "qemu";
  router.enable = true;
  nodes.sw1 = {
    imports = [ vivarium.kinds.sonic ];
    vivarium.sonic = images.sonic-vs;
  };
  phases.uplink = {
    script = ../phases/uplink-eth0.py;
    after = [ "boot" ];
  };
}

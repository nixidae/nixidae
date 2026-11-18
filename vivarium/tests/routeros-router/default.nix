/*
  RouterOS behind the router: its default DHCP client leases the
  uplink address from the router, and reaches the host through the
  router's relay.
*/
{
  vivarium,
  mkTest,
  images,
  ...
}:
mkTest {
  name = "routeros-router";
  backend = "qemu";
  router.enable = true;
  nodes.r1 = {
    imports = [ vivarium.kinds.routeros ];
    vivarium.routeros = images.routeros-chr;
  };
  phases.uplink = {
    script = ../phases/uplink-routeros.py;
    after = [ "boot" ];
  };
}

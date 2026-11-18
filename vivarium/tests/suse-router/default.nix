/*
  A guest that is not NixOS behind the router: openSUSE Leap's cloud
  image leases its uplink address from the router, and reaches the host
  through the router's relay.
*/
{
  vivarium,
  mkTest,
  images,
  ...
}:
mkTest {
  name = "suse-router";
  router.enable = true;
  nodes.suse = {
    imports = [ vivarium.kinds.image ];
    vivarium.image = images.opensuse-leap-16_0;
    vivarium.memory = "1024M";
  };
  phases.uplink = {
    script = ../phases/uplink-router.py;
    after = [ "boot" ];
  };
}

/*
  Two NixOS guests on two segments, routed through RouterOS. The
  router's interfaces carry their vivarium names and addresses.
*/
{
  vivarium,
  mkTest,
  images,
  lanHost,
  ...
}:
mkTest {
  name = "routeros-route";
  backend = "qemu";
  nodes.r1 = {
    imports = [ vivarium.kinds.routeros ];
    vivarium.routeros = images.routeros-chr;
    vivarium.interfaces = {
      left = {
        segment = "left";
        addresses = [ "10.1.0.1/24" ];
      };
      right = {
        segment = "right";
        addresses = [ "10.2.0.1/24" ];
      };
    };
  };
  nodes.a = lanHost "left" "10.1.0.2" "10.1.0.1" "10.2.0.0";
  nodes.b = lanHost "right" "10.2.0.2" "10.2.0.1" "10.1.0.0";
  phases.route = {
    script = ../phases/routeros-route.py;
    after = [ "boot" ];
  };
}

/*
  The router guest (nixidae/nixidae#1404): two guests behind one router,
  each with an address of its own on `vec0`, reaching the host through the
  router's relay and each other across its bridge. The host reaches each
  through the router's egress: `a` with every port, TCP and UDP, `b` with
  one.
*/
{ mkTest, ... }:
let
  answer =
    { pkgs, ... }:
    {
      environment.systemPackages = [
        pkgs.socat
        pkgs.dig
      ];
    };
in
mkTest {
  name = "router";
  router.enable = true;
  nodes.a = {
    imports = [ answer ];
    vivarium.forward = [
      {
        ports = "all";
        protocols = [
          "tcp"
          "udp"
        ];
      }
    ];
  };
  nodes.b = {
    imports = [ answer ];
    vivarium.forward = [
      {
        ports = [
          {
            host = null;
            guest = 7777;
          }
        ];
      }
    ];
  };
  phases.router = {
    script = ../router.py;
    after = [ "boot" ];
  };
}

/*
  The router guest (nixidae/nixidae#1404): two guests behind one router,
  each with an address of its own on `vec0`, reaching the host through the
  router's relay and each other across its bridge. The host reaches each
  through the router's egress: `a` with two TCP ports, one privileged, and
  a UDP port; `b` with one TCP port. router.py's TCP, PRIVILEGED and UDP.
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
    vivarium.inbound = [
      { guest = 7777; }
      { guest = 80; }
      {
        guest = 7778;
        protocols = [ "udp" ];
      }
    ];
  };
  nodes.b = {
    imports = [ answer ];
    vivarium.inbound = [ { guest = 7777; } ];
  };
  phases.router = {
    script = ../router.py;
    after = [ "boot" ];
  };
}

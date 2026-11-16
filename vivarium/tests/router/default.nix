/*
  The router guest (nixidae/nixidae#1404): two guests behind one router,
  each with an address of its own on `vec0`, reaching the host through the
  router's relay and each other across its bridge.
*/
{ mkTest, ... }:
mkTest {
  name = "router";
  router.enable = true;
  nodes.a = { };
  nodes.b = { };
  phases.router = {
    script = ../router.py;
    after = [ "boot" ];
  };
}

/*
  Does each guest see only its own closure, on every backend, and can
  it still add paths? `k` is the one with no bind-mount view: libkrun
  shares the host store with the closure as an allowlist.
*/
{ mkTest, ... }:
mkTest {
  name = "store-view";
  nodes.u.vivarium.backend = "uml";
  nodes.q.vivarium.backend = "qemu";
  nodes.c.vivarium.backend = "container";
  nodes.k.vivarium.backend = "krun";
  phases.view = {
    script = ../phases/store-view.py;
    after = [ "boot" ];
  };
}

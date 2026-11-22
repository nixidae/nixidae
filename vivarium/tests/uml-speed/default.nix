/*
  Do `vivarium.uml.smp` and `vivarium.uml.speed` reach the UML kernel?

  One guest on the patched SMP kernel with two processors: the guest
  sees two, and the speed patch's boot switches are on its command
  line. Not in CI: the kernel is a build of its own.
*/
{ mkTest, ... }:
mkTest {
  name = "uml-speed";
  backend = "uml";
  phases.test = {
    script = ../uml-speed.py;
    after = [ "boot" ];
  };
  nodes.one.vivarium = {
    cpus = 2;
    uml = {
      smp = true;
      speed = {
        enable = true;
        spin = 20;
        prefault = "64K";
      };
    };
  };
}

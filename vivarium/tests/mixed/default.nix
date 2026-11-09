/*
  One run, both kinds of guest: a UML guest and a QEMU guest on one
  segment. UML for what is single-threaded and wants to cost the host
  little, QEMU for what wants the CPU. A node sets its own
  `vivarium.backend`; the run's `backend` is only the default.

  By hand: the QEMU guest needs /dev/kvm, which the session job in CI
  does not have.
*/
{ mkTest, ... }:
mkTest {
  name = "mixed";
  nodes.small.vivarium = {
    backend = "uml";
    lan = {
      network = "mixed";
      address = "10.55.0.1/24";
    };
  };
  nodes.fast.vivarium = {
    backend = "qemu";
    lan = {
      network = "mixed";
      address = "10.55.0.2/24";
    };
  };
  phases.reach = {
    script = ../phases/mixed.py;
    after = [ "boot" ];
  };
}

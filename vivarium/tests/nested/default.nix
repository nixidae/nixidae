/*
  Can a QEMU guest run virtual machines of its own?

  By hand, not in CI: a GitHub runner is itself a VM, and its KVM
  does not nest a second time. Needs nesting on this host
  (`/sys/module/kvm_{intel,amd}/parameters/nested`).
*/
{ pkgs, mkTest, ... }:
mkTest {
  name = "nested";
  backend = "qemu";
  nodes.nested = {
    vivarium.nestedVirtualization = true;
    environment.systemPackages = [ pkgs.python3 ];
  };
  nodes.plain = { };
  phases.kvm = {
    script = ../phases/nested.py;
    after = [ "boot" ];
  };
}

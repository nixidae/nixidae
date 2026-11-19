/*
  An appliance: RouterOS CHR booted from MikroTik's image, beside a
  NixOS guest on one segment. Needs /dev/kvm, as QEMU does.
*/
{ vivarium, mkTest, ... }:
mkTest {
  name = "appliance";
  # `router` here is the appliance; the run's router guest, which the
  # appliance asks for, takes another name.
  router.name = "gateway";
  phases.test = {
    script = ../appliance.py;
    after = [ "boot" ];
  };
  nodes.router = {
    vivarium.backend = "qemu";
    imports = [ vivarium.kinds.appliance ];
    vivarium.appliance = vivarium.images.routeros-chr-7_24_5;
    vivarium.inbound = [ { guest = 80; } ];
    vivarium.interfaces.fabric.segment = "fabric";
  };
  nodes.peer.vivarium.interfaces.fabric.segment = "fabric";
}

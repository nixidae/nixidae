/*
  Does a QEMU guest come back from its own reboot, on the system its
  bootloader names? `next` changes the kernel parameters and the
  initrd, which only a new boot picks up. `two` must still reach it
  over their segment. `.krun` asks the same of libkrun guests. Not
  UML: a UML guest has no initrd, and its kernel is the process
  (nixidae/nixidae#1162).
*/
{ lib, mkTest, ... }:
mkTest (
  { systems, ... }:
  {
    name = "reboot";
    backend = "qemu";
    phases.test = {
      script = ../reboot.py;
      after = [ "boot" ];
    };
    defaults =
      { settings, ... }:
      {
        vivarium.lateClosure = [ settings.next ];
      };
    nodes = lib.genAttrs [ "one" "two" ] (name: {
      vivarium.lan = {
        network = "reboot";
        address = "10.105.0.${if name == "one" then "1" else "2"}/24";
      };
    });
    settings.next = "${(systems.one.extendModules {
      modules = [
        {
          boot.kernelParams = [ "vivarium.reboot=next" ];
          boot.initrd.systemd.contents."/etc/vivarium-reboot".text = "next\n";
          environment.etc."vivarium-reboot".text = "next\n";
        }
      ];
    }).config.system.build.toplevel
    }";
  }
)

/*
  Does a dinix guest boot under krun with no initrd and no NixOS, run the
  agent as a dinit service, and answer? See docs/design/dinix-guests.md.

  `slow` takes a second to start and boot waits for it, so a boot phase
  that returns before it started did not wait on dinit. `broken` is wired
  to nothing; the phase starts it, and `system_state` must name it.
*/
{
  vivarium,
  mkTest,
  pkgs,
  ...
}:
mkTest {
  name = "dinix";
  backend = "krun";
  nodes.box = {
    imports = [ vivarium.kinds.dinix ];
    vivarium.dinix.modules = [
      {
        services.slow = {
          type = "scripted";
          command = "${pkgs.coreutils}/bin/sleep 1";
          dinix.critical = false;
        };
        services.broken = {
          type = "scripted";
          command = "${pkgs.coreutils}/bin/false";
          dinix.critical = null;
        };
      }
    ];
  };
  phases.dinix = {
    script = ../phases/dinix.py;
    after = [ "boot" ];
  };
}

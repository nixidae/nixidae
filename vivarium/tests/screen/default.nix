/*
  Can a test read a guest's screen, type on it and point at it?

  `desk` has a screen with a getty on tty1; `blind` has none, the
  negative control. CI runs `screen.qemu`, where `blind` is QEMU too.
*/
{ mkTest, ... }:
mkTest {
  name = "screen";
  nodes.desk = {
    vivarium.backend = "qemu";
    vivarium.display.enable = true;
    services.getty.autologinUser = "root";
  };
  nodes.blind = { };
  phases.screen = {
    script = ../phases/screen.py;
    after = [ "boot" ];
  };
}

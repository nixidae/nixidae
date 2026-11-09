/*
  Does SONiC VS boot under QEMU and answer a shell on its serial
  console? Milestone 1 for a Linux-shell console guest. See
  docs/design/console-guests.md.
*/
{
  vivarium,
  mkTest,
  images,
  ...
}:
mkTest {
  name = "sonic";
  backend = "qemu";
  nodes.sw1 = {
    imports = [ vivarium.kinds.sonic ];
    vivarium.sonic = images.sonic-vs;
  };
  phases.sonic = {
    script = ../phases/sonic.py;
    after = [ "boot" ];
  };
}

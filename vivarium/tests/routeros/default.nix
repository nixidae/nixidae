/*
  Does RouterOS boot under QEMU, apply its config disk, and answer
  through its serial console? See docs/design/console-guests.md.
*/
{
  vivarium,
  mkTest,
  images,
  ...
}:
mkTest {
  name = "routeros";
  backend = "qemu";
  nodes.r1.imports = [ vivarium.kinds.routeros ];
  nodes.r1.vivarium.routeros = images.routeros-chr // {
    config = ''
      /system identity set name=r1
      /ip address add address=10.255.0.1/32 interface=lo
    '';
  };
  phases.routeros = {
    script = ../phases/routeros.py;
    after = [ "boot" ];
  };
}

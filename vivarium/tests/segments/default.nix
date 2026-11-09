/*
  Several segments per guest, each interface named as declared, on all
  three backends: `r` (UML) routes nothing between `a` and `b`; `q`
  (QEMU) and `c` (a container) also share `ll`, with link-local
  addresses only.

  By hand: the QEMU guest needs /dev/kvm and the container a tap.
*/
{ mkTest, ... }:
mkTest {
  name = "segments";
  nodes.r.vivarium = {
    backend = "uml";
    interfaces = {
      left = {
        segment = "a";
        addresses = [
          "10.70.1.1/24"
          "fd70:1::1/64"
        ];
      };
      right = {
        segment = "b";
        addresses = [ "10.70.2.1/24" ];
      };
    };
  };
  nodes.q.vivarium = {
    backend = "qemu";
    interfaces = {
      up = {
        segment = "a";
        addresses = [
          "10.70.1.2/24"
          "fd70:1::2/64"
        ];
      };
      spare.segment = "ll";
    };
  };
  nodes.c.vivarium = {
    backend = "container";
    interfaces = {
      down = {
        segment = "b";
        addresses = [ "10.70.2.2/24" ];
      };
      side.segment = "ll";
    };
  };
  phases.reach = {
    script = ../phases/segments.py;
    after = [ "boot" ];
  };
}

/*
  Does VyOS boot under UML, from its ISO's squashfs, and apply its
  config.boot? See modules/vyos-guest.nix.
*/
{
  vivarium,
  mkTest,
  images,
  ...
}:
mkTest {
  name = "vyos-uml";
  nodes.vyos = {
    imports = [ vivarium.kinds.vyos ];
    vivarium.vyos = images.vyos-rolling // {
      config = ''
        interfaces {
            dummy dum0 {
                address "10.255.0.1/32"
            }
            loopback lo {
            }
        }
        system {
            host-name "vyos1"
        }
      '';
    };
    vivarium.memory = "1024M";
  };
  phases.vyos = {
    script = ../phases/vyos.py;
    after = [ "boot" ];
  };
}

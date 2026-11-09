/*
  One guest of each backend on one segment, reaching each other over
  IP by name. `mixed` and `container-lan` each prove one pair; this
  holds all three at once.
*/
{ lib, mkTest, ... }:
mkTest {
  name = "backends";
  nodes =
    lib.mapAttrs
      (name: backend: {
        vivarium = {
          inherit backend;
          lan = {
            network = "backends";
            address =
              {
                u = "10.57.0.1/24";
                q = "10.57.0.2/24";
                c = "10.57.0.3/24";
              }
              .${name};
          };
        };
      })
      {
        u = "uml";
        q = "qemu";
        c = "container";
      };
  phases.reach = {
    script = ../phases/backends.py;
    after = [ "boot" ];
  };
}

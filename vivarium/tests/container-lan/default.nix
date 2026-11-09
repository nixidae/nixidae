# Two containers and a UML guest on one segment. By hand, as above.
{ lib, mkTest, ... }:
mkTest {
  name = "container-lan";
  nodes =
    lib.mapAttrs
      (name: value: {
        vivarium = {
          backend = if name == "u" then "uml" else "container";
          lan = {
            network = "clan";
            address = "${value}/24";
          };
        };
      })
      {
        a = "10.56.0.1";
        b = "10.56.0.2";
        u = "10.56.0.3";
      };
  phases.reach = {
    script = ../phases/container-lan.py;
    after = [ "boot" ];
  };
}

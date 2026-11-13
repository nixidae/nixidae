/*
  The runner's hub cuts and captures a segment, on every backend.
  `pair` is captured from boot, which makes it a hub. `tri` has three
  members, so it is a hub anyway: one member is cut and restored.
  `direct` is a plain pair, and refuses a cut.
*/
{ mkTest, ... }:
let
  guest = n: extra: {
    vivarium.interfaces = {
      tri = {
        segment = "tri";
        addresses = [ "10.82.1.${toString n}/24" ];
      };
    }
    // extra;
  };
  pair = n: {
    pair = {
      segment = "pair";
      addresses = [ "10.82.2.${toString n}/24" ];
    };
    direct = {
      segment = "direct";
      addresses = [ "10.82.3.${toString n}/24" ];
    };
  };
in
mkTest {
  name = "segment-cut";
  segments.pair.capture = true;
  nodes.a = guest 1 (pair 1);
  nodes.b = guest 2 (pair 2);
  nodes.c = guest 3 { };
  phases.cut = {
    script = ../phases/segment-cut.py;
    after = [ "boot" ];
  };
}

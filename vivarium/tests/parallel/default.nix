/*
  Do phases on disjoint guests run at once, and is everything they
  say still filed under the right phase?

  `left` holds `a`, `right` holds `b`, and `both` holds every guest
  and comes after them. Three claims, each read from events.jsonl:

  - `left` and `right` overlap in time.
  - `both` overlaps nothing: a phase without `nodes` runs alone. This
    is the negative control, and what a scheduler that ignored
    `nodes` would fail.
  - Every journal marker and every print carries the phase that held
    its guest, although two phases were writing at once.
*/
{ pkgs, mkTest, ... }:
let
  run = mkTest {
    name = "parallel";
    nodes.a = { };
    nodes.b = { };
    phases = {
      left = {
        script = ../phases/parallel.py;
        nodes = [ "a" ];
        after = [ "boot" ];
      };
      right = {
        script = ../phases/parallel.py;
        nodes = [ "b" ];
        after = [ "boot" ];
      };
      both = {
        script = ../phases/parallel.py;
        after = [
          "left"
          "right"
        ];
      };
    };
  };
in
pkgs.runCommand "vivarium-check-parallel" { passthru.session = run; } ''
  ${pkgs.python3.interpreter} ${../checks}/parallel.py ${run.attempt}
  touch $out
''

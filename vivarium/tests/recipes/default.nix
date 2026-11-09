/*
  Does evidence get collected on the run that went wrong?

  `boot` is the standard library's recipe, which nothing here
  declares. `evidence` is ordered after a phase that fails on
  purpose, and `after` alone would skip it for exactly that reason.
  `always` is what separates "run me later" from "do not bother if
  that failed".

  Also checks the failure does not pass *through* `evidence`: `later`
  needs only `evidence`, so it still runs.
*/
{ pkgs, mkTest, ... }:
let
  run = mkTest {
    name = "recipes";
    nodes.one = { };
    phases = {
      cluster = {
        script = ../phases/cluster.py;
        after = [ "boot" ];
      };
      evidence = {
        script = ../phases/evidence.py;
        after = [ "cluster" ];
        always = true;
      };
      later = {
        script = ../phases/independent.py;
        after = [ "evidence" ];
      };
    };
  };
in
pkgs.runCommand "vivarium-check-recipes" { passthru.session = run; } ''
  ${pkgs.python3.interpreter} ${../checks}/recipes.py ${run.attempt}
  touch $out
''

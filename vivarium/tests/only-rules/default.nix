/*
  Can a caller run one phase and leave the rest alone?

  `--only` is the fast door: boot once, do the one thing being worked
  on, and exit 0 when it passed. A developer who asked for one phase
  knows the rest did not run.

  The safety property is that only a *caller* can do this. The check
  passes no `--only`, so CI cannot go green by running a subset --
  and the session below proves it, because one of its phases fails on
  purpose and building it whole fails.

  `deselected` is therefore a different state from `skipped`. Skipped
  means nobody knows the answer and the run failed; deselected means
  nobody wanted it.
*/
{
  pkgs,
  lib,
  mkTest,
  cli,
  ...
}:
let
  run = mkTest {
    name = "only";
    nodes.one = { };
    phases = {
      boot.script = ../phases/boot.py;
      only = {
        script = ../phases/only.py;
        after = [ "boot" ];
        # Carried through the spec; a pass says the limit is not hit.
        timeout = 300;
      };
      # Each fails if it ever runs, which is the point: `--only`
      # must not reach either, and building this session whole must
      # fail. `--only` takes what `only` runs after, never what runs
      # after it.
      never = {
        script = ../phases/check.py;
        after = [ "boot" ];
      };
      later = {
        script = ../phases/check.py;
        after = [ "only" ];
      };
    };
  };
in
pkgs.runCommand "vivarium-check-only" { passthru.session = run; } ''
  ${pkgs.python3.interpreter} ${../checks}/only.py ${lib.getExe cli} ${lib.getExe run.driver} "$TMPDIR"
  touch $out
''

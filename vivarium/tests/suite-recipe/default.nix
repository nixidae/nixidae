/*
  Does the `suites` recipe run pytest inside a guest?

  The suite holds one passing and one failing test. The check's own
  run fails, and both become cases with the guest's name. A driver
  run given `-- -k test_ok` passes, so the driver's arguments reach
  the pytest in the guest.
*/
{
  pkgs,
  lib,
  mkTest,
  ...
}:
let
  run = mkTest {
    name = "suite-recipe";
    nodes.one = { };
    vivarium.recipes.suites.inner = {
      node = "one";
      tests = ".";
      directory = "${../guest-suite}";
      pytest = "${pkgs.python3.withPackages (p: [ p.pytest ])}/bin/pytest";
      timeout = 600;
    };
  };
in
pkgs.runCommand "vivarium-check-suite-recipe" { passthru.session = run; } ''
  ${pkgs.python3.interpreter} ${../checks}/suite_recipe.py ${run.attempt} ${lib.getExe run.driver} "$TMPDIR"
  touch $out
''

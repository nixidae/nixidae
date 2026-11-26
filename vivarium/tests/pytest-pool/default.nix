# The run's pool (nixidae/nixidae#1413, vivarium/pool.py): clean scopes
# get fresh guests of a template, dirty scopes share one, and a test runs
# inside a pooled guest. The guests are dinix container guests, booted
# ahead as spares. With `xdist`, two pytest-xdist workers share the pool.
{
  pkgs,
  vivarium,
  mkTest,
  xdist ? false,
  ...
}:
vivarium.pytestCheck {
  name = if xdist then "pytest-pool-xdist" else "pytest-pool";
  src = if xdist then ./xdist else ./project;
  runs.boxes = mkTest {
    name = "boxes";
    backend = "container";
    templates.box = {
      imports = [ vivarium.kinds.dinix ];
      vivarium.dinix = { };
    };
  };
  pythonPackages = pkgs.lib.optional xdist pkgs.python3Packages.pytest-xdist;
  args = pkgs.lib.optionals xdist [
    "--numprocesses=2"
    "--dist=loadgroup"
  ];
}

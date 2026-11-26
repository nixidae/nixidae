# The run's pool (nixidae/nixidae#1413, vivarium/pool.py): clean scopes
# get fresh guests of a template, dirty scopes share one, and a test runs
# inside a pooled guest. The guests are dinix container guests, booted
# ahead as spares.
{
  vivarium,
  mkTest,
  ...
}:
vivarium.pytestCheck {
  name = "pytest-pool";
  src = ./project;
  runs.boxes = mkTest {
    name = "boxes";
    backend = "container";
    templates.box = {
      imports = [ vivarium.kinds.dinix ];
      vivarium.dinix = { };
    };
  };
}

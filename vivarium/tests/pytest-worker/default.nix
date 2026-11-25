# The guest's pytest worker, as a user drives it (nixidae/nixidae#1413):
# `-x` stops the guest too, a collection error in the guest shows its
# text, and a passing test's output reaches the host.
{
  pkgs,
  sources,
  vivarium,
  ...
}:
vivarium.pytestCheck {
  name = "pytest-worker";
  src = ./project;
  runs = {
    inherit (import ../../examples/pytest-host/machines.nix { inherit sources pkgs; }) one;
  };
  args = [
    "-p"
    "pytester"
  ];
}

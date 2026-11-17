# examples/pytest-host's suite in the sandbox: plain pytest drives the
# guests, each run from a prebuilt `.up` (nixidae/nixidae#1413).
{
  pkgs,
  sources,
  vivarium,
  ...
}:
vivarium.pytestCheck {
  name = "pytest-host";
  src = ../../examples/pytest-host;
  runs = import ../../examples/pytest-host/machines.nix { inherit sources pkgs; };
}

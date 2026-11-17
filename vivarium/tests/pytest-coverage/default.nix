# Coverage from tests that run in a guest reaches the host's pytest-cov
# (nixidae/nixidae#1413). Only a test in the guest runs `sample.where`, so
# 100% of `sample` means the guest's data was combined with the host's.
{
  pkgs,
  sources,
  vivarium,
  ...
}:
vivarium.pytestCheck {
  name = "pytest-coverage";
  src = ../../examples/pytest-host;
  runs = {
    inherit (import ../../examples/pytest-host/machines.nix { inherit sources pkgs; }) one;
  };
  pythonPackages = [ pkgs.python3Packages.pytest-cov ];
  args = [
    "--cov=sample"
    "--cov-report=term-missing"
    "--cov-fail-under=100"
    "test_in_guest.py"
  ];
}

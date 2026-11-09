# The run's and a guest's `python.packages` reach the phase and its
# type check, in one venv.
{ pkgs, mkTest, ... }:
mkTest {
  name = "python-packages";
  nodes.one.vivarium.python.packages = [ pkgs.python3Packages.humanize ];
  python.packages = [ pkgs.python3Packages.tomli-w ];
  phases = {
    boot.script = ../phases/boot.py;
    imports = {
      script = ../phases/python-packages.py;
      after = [ "boot" ];
    };
  };
}

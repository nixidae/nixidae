# The scripts in tests/, against the library they drive. Also the
# check that `typeCheck` itself works.
{
  pkgs,
  lib,
  vivarium,
  k8s,
  ...
}:
vivarium.typeCheck {
  name = "own-scripts";
  # `.py` only: a run outside the sandbox leaves `__pycache__` beside
  # them, and zuban has nothing to say about a `.pyc`.
  scripts = lib.filter (p: lib.hasSuffix ".py" (toString p)) (
    # `recipes` as well as `tests`: a recipe is shipped for other
    # projects to enable, so one that does not type check breaks a
    # consumer rather than this repository.
    lib.filesystem.listFilesRecursive ../../recipes ++ lib.filesystem.listFilesRecursive ../../tests
  );
  # What `python-packages` gives its phase, and what the cluster tests
  # import.
  extraPackages = [
    pkgs.python3Packages.humanize
    pkgs.python3Packages.tomli-w
    k8s
  ];
  # The check scripts import their shared `harness` by name.
  extraPaths = [ ../checks ];
}

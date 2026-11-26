# pytest-worker's project on a dinix guest in a container: dinit is PID 1,
# with no systemd and no NixOS, and the guest runs the tests
# (nixidae/nixidae#1413, docs/design/dinix-guests.md).
# tests/pytest-worker-dinix-krun calls it with `backend = "krun"`.
{
  lib,
  vivarium,
  mkTest,
  backend ? "container",
  ...
}:
vivarium.pytestCheck {
  name = "pytest-worker-dinix" + lib.optionalString (backend != "container") "-${backend}";
  # pytest-worker's tests, and one that asks the guest for its PID 1.
  src = lib.fileset.toSource {
    root = ../.;
    fileset = lib.fileset.unions [
      ../pytest-worker/project
      ./project
    ];
  };
  runs.one = mkTest {
    name = "one";
    inherit backend;
    nodes.vm = {
      imports = [ vivarium.kinds.dinix ];
      vivarium.dinix = { };
    };
  };
  args = [
    "-p"
    "pytester"
  ];
}

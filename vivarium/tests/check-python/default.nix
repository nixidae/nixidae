# zuban, strict, over `runner` and `session` and their unit tests. Each
# package's `mypy.ini` sets its own floor.
{
  hostPkgs,
  lib,
  nixvenv,
  runner,
  session,
  k8s,
  ...
}:
let
  python = nixvenv {
    python = hostPkgs.python3;
    packages = ps: [
      runner
      session
      k8s
      ps.pytest
    ];
  };
  # What zuban reads of a package.
  typed =
    package:
    lib.fileset.toSource {
      root = package;
      fileset = lib.fileset.fileFilter (
        file: file.hasExt "py" || file.name == "py.typed" || file.name == "mypy.ini"
      ) package;
    };
in
hostPkgs.runCommand "vivarium-check-python" { nativeBuildInputs = [ hostPkgs.zuban ]; } ''
  for package in ${typed ../../pkgs/vivarium-runner} ${typed ../../pkgs/vivarium} ${typed ../../pkgs/vivarium-k8s}; do
    # Linux on any host: CI's verdict, and container.py is Linux only.
    (cd $package && zuban mypy --platform linux --python-executable ${python}/lib/venv/bin/python3)
  done
  touch $out
''

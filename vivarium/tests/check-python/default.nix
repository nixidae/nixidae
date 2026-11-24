# zuban, strict, over `agent`, `runner`, `session`, `k8s` and the router's
# programs, and their unit tests. Each package's `mypy.ini` sets its own floor.
{
  hostPkgs,
  lib,
  nixvenv,
  agent,
  runner,
  session,
  k8s,
  ...
}:
let
  socks = hostPkgs.callPackage ../../pkgs/vivarium-socks { };
  python = nixvenv {
    python = hostPkgs.python3;
    packages = ps: [
      agent
      runner
      session
      k8s
      socks
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
  for package in ${typed ../../pkgs/vivarium-agent} ${typed ../../pkgs/vivarium-runner} ${typed ../../pkgs/vivarium} ${typed ../../pkgs/vivarium-k8s} ${typed ../../pkgs/vivarium-socks}; do
    # Linux on any host: CI's verdict, and container.py is Linux only.
    (cd $package && zuban mypy --platform linux --python-executable ${python}/lib/venv/bin/python3)
  done
  touch $out
''

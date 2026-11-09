# zuban, strict, over `runner` and `session` and their unit tests. Each
# package's `mypy.ini` sets its own floor.
{
  pkgs,
  nixvenv,
  runner,
  session,
  k8s,
  ...
}:
let
  python = nixvenv {
    python = pkgs.python3;
    packages = ps: [
      runner
      session
      k8s
      ps.pytest
    ];
  };
in
pkgs.runCommand "vivarium-check-python" { nativeBuildInputs = [ pkgs.zuban ]; } ''
  for package in ${../../pkgs/vivarium-runner} ${../../pkgs/vivarium} ${../../pkgs/vivarium-k8s}; do
    (cd $package && zuban mypy --python-executable ${python}/lib/venv/bin/python3)
  done
  touch $out
''

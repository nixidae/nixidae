# An environment from a lock file in which `idna` is an sdist only: its
# backend, `flit_core`, and the installer are nodes of the same graph.
# `myapp` is a local project, installed as a PEP 660 editable.
{
  pydd,
  runCommand,
}:
let
  env = pydd.python.mkEnvironment {
    name = "demo-venv";
    lock = builtins.fromJSON (builtins.readFile ./lock.json);
    editables.myapp = {
      src = ./fixtures/myapp;
      root = "$PYDD_EDITABLE_ROOT";
    };
  };
in
env
// {
  # The environment, run from inside: imports, a console script from an
  # sdist, a binary wheel, and the editable reading a tree outside the store.
  check = runCommand "demo-venv-check" { PYDD_EDITABLE_ROOT = "${./fixtures/myapp}"; } ''
    {
      "${env.out}/bin/python" ${./check.py}
      echo "ninja $("${env.out}/bin/ninja" --version)"
      echo "idna $("${env.out}/bin/idna" ドメイン.テスト)"
      echo "myapp $("${env.out}/bin/myapp")"
    } > "$out"
  '';
}

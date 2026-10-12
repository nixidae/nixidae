# An environment from the uv.lock of `myapp`, a local project installed as a
# PEP 660 editable. `idna` is built from its sdist, and a planner of its own
# reads the backend out of the archive. That backend, `flit_core`, and the
# installer come from the same lock, and are nodes of the same graph.
{
  pydd,
  runCommand,
}:
let
  env = pydd.python.uv {
    project = ./fixtures/myapp;
    venvName = "demo-venv";
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

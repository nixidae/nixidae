# An environment from the uv.lock of `myapp`, a local project installed as a
# PEP 660 editable. `idna` is built from its sdist: its backend, `flit_core`,
# and the installer come from the same lock, and are nodes of the same graph.
{
  pydd,
  runCommand,
}:
let
  env = pydd.python.mkEnvironment {
    name = "demo-venv";
    src = ./fixtures/myapp;
    editableRoots.myapp = "$PYDD_EDITABLE_ROOT";
    noBinary = [ "idna" ];
    buildSystems.idna = {
      backend = "flit_core.buildapi";
      requires = [ "flit_core >=3.11,<5" ];
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

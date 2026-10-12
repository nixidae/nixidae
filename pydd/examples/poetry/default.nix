# The environment of `examples/venv`, from a `poetry.lock` instead of a
# `uv.lock`. The planner and `graph.nix` are the same; only the reader of the
# lock file differs. `idna` builds from its sdist, through a planner of its own.
{
  pydd,
  runCommand,
}:
let
  env = pydd.python.poetry {
    project = ./fixtures/myapp;
    venvName = "demo-poetry";
    noBinary = [ "idna" ];
  };
in
env
// {
  check = runCommand "demo-poetry-check" { PYDD_EDITABLE_ROOT = "${./fixtures/myapp}"; } ''
    {
      "${env.out}/bin/python" ${../venv/check.py}
      echo "idna $("${env.out}/bin/idna" ドメイン.テスト)"
      echo "myapp $("${env.out}/bin/myapp")"
    } > "$out"
  '';
}

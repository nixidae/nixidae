{
  lib,
  runCommand,
  devEnv,
  ruff,
  zuban,
  nixpkgsPath,
}:
let
  src = lib.fileset.toSource {
    root = ../.;
    fileset = lib.fileset.unions [
      ../README.md
      ../mypy.ini
      ../pytest.ini
      ../ruff.toml
      ./shared/ruff.toml
      ./shared/ruff-anyio.toml
      ../modiff/pyproject.toml
      ../modiff/src
      (lib.fileset.fileFilter (file: file.hasExt "py") ../tests)
    ];
  };

  check =
    name: script:
    runCommand "modiff-${name}"
      {
        nativeBuildInputs = [
          devEnv
          ruff
          zuban
        ];
        # The toy configurations import `<nixpkgs/lib>`.
        NIX_PATH = "nixpkgs=${nixpkgsPath}";
      }
      ''
        cp -r ${src}/. .
        chmod -R u+w .
        export HOME=$TMPDIR
        ${script}
        touch $out
      '';
in
{
  pytest = check "pytest" "pytest -p no:cacheprovider";
  lint = check "lint" "ruff check --no-cache";
  format = check "format" "ruff format --check --no-cache";
  types = check "types" "zuban mypy";
}

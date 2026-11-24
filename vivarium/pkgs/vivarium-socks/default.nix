{
  lib,
  python3Packages,
}:

# A package, not an application: `check-python` types it in a venv, which
# needs its dependencies propagated.
python3Packages.buildPythonPackage {
  pname = "vivarium-socks";
  version = "0.1.0";

  src = lib.fileset.toSource {
    root = ./.;
    fileset = lib.fileset.unions [
      ./pyproject.toml
      ./vivarium_socks
      ./tests
    ];
  };

  pyproject = true;

  build-system = [ python3Packages.hatchling ];
  dependencies = [
    python3Packages.anyio
    python3Packages.dnspython
  ];

  nativeCheckInputs = [ python3Packages.pytestCheckHook ];
  pythonImportsCheck = [ "vivarium_socks" ];

  meta = {
    description = "A SOCKS5 proxy into a vivarium lab";
    license = lib.licenses.asl20;
    mainProgram = "vivarium-socks";
    platforms = lib.platforms.unix;
  };
}

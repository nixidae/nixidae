{
  lib,
  python3Packages,
}:

python3Packages.buildPythonPackage {
  pname = "vivarium-ca";
  version = "0.1.0";

  src = lib.fileset.toSource {
    root = ./.;
    fileset = lib.fileset.unions [
      ./pyproject.toml
      ./vivarium_ca
      ./tests
    ];
  };

  pyproject = true;

  build-system = [ python3Packages.hatchling ];
  dependencies = [ python3Packages.cryptography ];

  nativeCheckInputs = [ python3Packages.pytestCheckHook ];
  pythonImportsCheck = [ "vivarium_ca" ];

  meta = {
    description = "A name-constrained lab CA for vivarium runs";
    license = lib.licenses.asl20;
    platforms = lib.platforms.linux ++ lib.platforms.darwin;
  };
}

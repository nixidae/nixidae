{ lib, python3Packages }:

python3Packages.buildPythonPackage {
  pname = "vivarium-runner";
  version = "0.2.0";

  src = lib.fileset.toSource {
    root = ./.;
    fileset = lib.fileset.unions [
      ./pyproject.toml
      ./vivarium_runner
      ./tests
    ];
  };

  pyproject = true;

  build-system = [ python3Packages.hatchling ];
  dependencies = [
    python3Packages.rpyc
    # ConsoleControl. The rest of the runner is bare asyncio until
    # nixidae/nixidae#949.
    python3Packages.anyio
    # QEMU's own monitor client, rather than a second implementation of
    # the greeting, the capabilities handshake and the difference between
    # an event and a reply. Only the QEMU backend imports it.
    python3Packages.qemu-qmp
  ];

  pythonImportsCheck = [ "vivarium_runner" ];

  # What needs no guest: the console dialects, against recorded screens.
  nativeCheckInputs = [ python3Packages.pytestCheckHook ];

  meta = {
    description = "Run NixOS systems under User-Mode Linux and drive them from Python";
    license = lib.licenses.asl20;
    platforms = lib.platforms.linux;
    mainProgram = "vivarium-run";
  };
}

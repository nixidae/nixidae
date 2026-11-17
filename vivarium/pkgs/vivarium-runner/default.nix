{
  lib,
  python3Packages,
  dinit-client,
}:

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
    # The agent's init on a dinix guest. Only the agent's dinit calls use it.
    dinit-client
  ];

  pythonImportsCheck = [ "vivarium_runner" ];

  # What needs no guest: the console dialects, against recorded screens.
  nativeCheckInputs = [ python3Packages.pytestCheckHook ];
  # The forward tests bind 127.0.0.1, which the darwin sandbox refuses.
  __darwinAllowLocalNetworking = true;

  meta = {
    description = "Run NixOS systems as UML, QEMU, libkrun or crun guests and drive them from Python";
    license = lib.licenses.asl20;
    platforms = lib.platforms.linux ++ lib.platforms.darwin;
    mainProgram = "vivarium-run";
  };
}

# SPDX-License-Identifier: MIT

{
  buildPythonPackage,
  buildGoModule,
  hatchling,
  grpclib-ttrpc,
  lib,
  nri-proto-python,
  pytestCheckHook,
  pytest-asyncio,
  typing-extensions,
}:
let
  pyproject = builtins.fromTOML (builtins.readFile ./pyproject.toml);

  testServer = buildGoModule {
    pname = "nri-test-server";
    version = "0.1.0";

    src = ./go;
    vendorHash = "sha256-M5N/V51YeP9lplKsDL7faI4zzBPIRx24eqJQfEzIrUY=";

    ldflags = [
      "-s"
      "-w"
    ];

    meta.mainProgram = "grpclib-nri-test-server";
  };
in
buildPythonPackage {
  pname = pyproject.project.name;
  version = pyproject.project.version;

  src = ./.;
  pyproject = true;
  build-system = [ hatchling ];

  dependencies = [
    grpclib-ttrpc
    nri-proto-python
    typing-extensions
  ];

  nativeCheckInputs = [
    pytestCheckHook
    pytest-asyncio
    testServer
  ];

  preCheck = ''
    export NRI_TEST_SERVER="${lib.getExe testServer}"
  '';

  passthru = {
    inherit testServer;
  };
}

# SPDX-License-Identifier: MIT

{
  buildPythonPackage,
  hatchling,
  grpclib,
  multidict,
  fetchFromGitHub,
  mypy-protobuf,
  grpcio-tools,
  protobuf,
  ttrpc-proto-python,
  pytest,
  pytest-asyncio,
}:
let
  pyproject = builtins.fromTOML (builtins.readFile ./pyproject.toml);
in
buildPythonPackage {
  pname = pyproject.project.name;
  version = pyproject.project.version;

  src = ./.;
  pyproject = true;
  build-system = [ hatchling ];

  dependencies = [
    protobuf
    grpclib
    multidict
    ttrpc-proto-python
  ];

  nativeBuildInputs = [
    protobuf
    grpclib
    mypy-protobuf
    grpcio-tools
  ];

  nativeCheckInputs = [
    pytest
    pytest-asyncio
  ];
}

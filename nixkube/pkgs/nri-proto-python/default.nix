# SPDX-License-Identifier: MIT

# Credits to Claude Sonnet 4.6
{
  lib,
  buildPythonPackage,
  fetchFromGitHub,
  hatchling,
  grpcio-tools,
  grpclib,
  protobuf,
}:
let
  version = "0.11.0";
  nri = fetchFromGitHub {
    owner = "containerd";
    repo = "nri";
    rev = "1078130fa016884b4c03880d9d587e6691a67d98";
    sha256 = "sha256-E3UivHF+tTltMUrdgQk2rIJGtqOav4iqF1E3sYXsoGU=";
  };
in
buildPythonPackage {
  inherit version;
  pname = "nri-proto-python";

  src = ./.;

  build-system = [ hatchling ];
  nativeBuildInputs = [
    grpclib
    grpcio-tools
  ];

  dependencies = [
    grpclib
    protobuf
  ];

  format = "pyproject";
  preBuild = ''
    mkdir -p src/nri
    touch src/nri/py.typed
    touch src/nri/__init__.py
    protoc \
      --proto_path="${nri}/pkg/api" \
      --python_out="src/nri" \
      --grpclib_python_out="src/nri" \
      api.proto

    substituteInPlace src/nri/api_grpc.py \
      --replace-fail "import api_pb2" "from . import api_pb2"
  '';

  meta = with lib; {
    description = "Python gRPC/protobuf library for containerd NRI (Node Resource Interface)";
    homepage = "https://github.com/containerd/nri";
    license = licenses.asl20;
    platforms = platforms.all;
  };
}

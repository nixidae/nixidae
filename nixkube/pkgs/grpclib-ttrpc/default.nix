# SPDX-License-Identifier: MIT

{
  buildPythonPackage,
  hatchling,
  grpclib,
  multidict,
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
    grpclib
    multidict
  ];
}

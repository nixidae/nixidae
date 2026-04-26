# SPDX-License-Identifier: MIT

{
  buildPythonApplication,
  hatchling,
  pynixd,
  kr8s,
}:
let
  pyproject = builtins.fromTOML (builtins.readFile ./pyproject.toml);
in
buildPythonApplication {
  pname = pyproject.project.name;
  version = pyproject.project.version;
  src = ./.;
  pyproject = true;
  build-system = [ hatchling ];
  dependencies = [
    pynixd
    kr8s
  ];
  meta.mainProgram = "pynixd-nixkube";
}

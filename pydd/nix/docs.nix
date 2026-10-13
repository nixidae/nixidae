{
  lib,
  stdenvNoCC,
  python,
  sphinx,
  myst-parser,
  furo,
  graphviz,
}:
let
  pythonEnv = python.withPackages (_: [
    sphinx
    myst-parser
    furo
  ]);
in
stdenvNoCC.mkDerivation {
  pname = "pydd-docs";
  version = "0";

  src = lib.fileset.toSource {
    root = ../.;
    fileset = ../docs;
  };

  nativeBuildInputs = [
    pythonEnv
    graphviz
  ];

  buildPhase = ''
    runHook preBuild
    sphinx-build -b html docs "$out" -W
    runHook postBuild
  '';

  dontInstall = true;
}

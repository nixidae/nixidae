{
  lib,
  stdenvNoCC,
  python,
  sphinx,
  myst-parser,
  furo,
  nixvenv,
}:
let
  pythonEnv = nixvenv {
    inherit python;
    packages = _: [
      sphinx
      myst-parser
      furo
    ];
    executables = [ "sphinx-build" ];
  };
in
stdenvNoCC.mkDerivation {
  pname = "pynix-review-docs";
  version = "0";

  # `docs/index.md` includes the README.
  src = lib.fileset.toSource {
    root = ../.;
    fileset = lib.fileset.unions [
      ../README.md
      ../docs
      ../nix/shared/sphinx_shared.py
    ];
  };

  nativeBuildInputs = [ pythonEnv ];

  buildPhase = ''
    runHook preBuild
    NIXIDAE_DOCS_OFFLINE=1 sphinx-build -b html docs "$out" -W
    runHook postBuild
  '';

  dontInstall = true;
}

{
  lib,
  stdenvNoCC,
  python,
  sphinx,
  myst-parser,
  furo,
  nixvenv,
  testOptions,
  guestOptions,
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
  pname = "vivarium-docs";
  version = "0";

  src = lib.fileset.toSource {
    root = ../.;
    fileset = lib.fileset.unions [
      ../docs
      ../nix/shared/sphinx_shared.py
    ];
  };

  nativeBuildInputs = [ pythonEnv ];

  buildPhase = ''
    runHook preBuild
    cp ${testOptions} docs/test-options.md
    cp ${guestOptions} docs/guest-options.md
    NIXIDAE_DOCS_OFFLINE=1 sphinx-build -b html docs "$out" -W
    runHook postBuild
  '';

  dontInstall = true;
}

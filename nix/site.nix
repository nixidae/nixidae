# The nixidae site: a landing page, and the docs site of each source under
# `<source>/`. nixidae/nixidae#1105.
{
  lib,
  stdenvNoCC,
  python,
  sphinx,
  myst-parser,
  furo,
  nixvenv,
  # Each source with a site, as { description; site; }.
  sites,
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

  # HTML and not a Markdown list: MyST reads a relative Markdown link as a
  # cross-reference, and finds no page for a site it did not build. The links
  # are relative, so that the site works at any base URL.
  list = ''
    <ul>
    ${
      lib.concatStrings (
        lib.mapAttrsToList (name: source: ''
          <li><a href="${name}/index.html">${name}</a>: ${source.description}</li>
        '') sites
      )
    }</ul>
  '';
in
stdenvNoCC.mkDerivation {
  pname = "nixidae-site";
  version = "0";

  src = lib.fileset.toSource {
    root = ../.;
    fileset = lib.fileset.unions [
      ../docs
      ./shared/sphinx_shared.py
    ];
  };

  nativeBuildInputs = [ pythonEnv ];

  inherit list;
  passAsFile = [ "list" ];

  buildPhase = ''
    runHook preBuild
    cp "$listPath" docs/sources.md
    NIXIDAE_DOCS_OFFLINE=1 sphinx-build -b html docs "$out" -W
    runHook postBuild
  '';

  # Copies, not links: a Pages artifact keeps a link as a link, and a link
  # into the store points nowhere on the host that serves it.
  installPhase = ''
    runHook preInstall
    ${lib.concatStrings (
      lib.mapAttrsToList (name: source: ''
        cp -r --no-preserve=mode ${source.site} "$out/${name}"
      '') sites
    )}
    runHook postInstall
  '';
}

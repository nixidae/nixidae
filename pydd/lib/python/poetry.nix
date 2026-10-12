# A Poetry project, built as a graph of derivations.
#
# **This file makes no packaging decision**, as `./uv.nix` makes none. It reads
# `poetry.lock` with `builtins.fromTOML`, makes one `pkgs.fetchurl` for every
# file of every package, and gives that list to `./planner.nix`.
# `pydd.python._poetrylock` decides, inside the planner, which packages this
# target installs.
#
# **`poetry.lock` records no URL**, only a file name and a hash. PyPI serves a
# file at `/packages/<python tag>/<first letter>/<project>/<file>`, and an
# sdist under the tag `source`. So this reader takes packages of PyPI only.
{
  pkgs,
  # The pydd library, with the huggorm writer.
  pydd,
  # The Poetry project to build. It holds `pyproject.toml` and `poetry.lock`.
  project,
  # The dependency groups to install. `null` is the runtime dependencies,
  # which is what `poetry install --only main` installs.
  groups ? null,
  # The extras of the project to enable.
  extras ? [ ],
  # The installer, which a dependency group puts in the lock.
  installer ? "installer",
  # What an editable install of the project redirects to.
  editableRoot ? "$PYDD_EDITABLE_ROOT",
  # Which form to install the project in: `editable`, `wheel` or `sdist`.
  mode ? "editable",
  # Packages to build from their sdist even when a wheel fits.
  noBinary ? [ ],
  venvName ? null,
  python ? pkgs.python3,
  markers ? { },
  buildTools ? { },
  fromNixpkgs ? { },
}:
let
  inherit (pkgs) lib;

  pyproject = builtins.fromTOML (builtins.readFile (project + "/pyproject.toml"));
  lockFile = project + "/poetry.lock";
  document = builtins.fromTOML (builtins.readFile lockFile);

  poetryTable = pyproject.tool.poetry or { };
  projectName = pyproject.project.name or poetryTable.name;
  environmentName = if venvName != null then venvName else projectName;
  # `package-mode = false` is a project that only holds dependencies.
  packageMode = poetryTable."package-mode" or true;

  # The python tag of a wheel is the third field from the end of its name,
  # which holds whether or not the wheel carries a build tag.
  pythonTag =
    file:
    let
      fields = lib.splitString "-" (lib.removeSuffix ".whl" file);
    in
    builtins.elemAt fields (builtins.length fields - 3);

  isWheel = file: lib.hasSuffix ".whl" file;

  urlOf =
    name: file:
    "https://files.pythonhosted.org/packages/${
      if isWheel file then pythonTag file else "source"
    }/${builtins.substring 0 1 name}/${name}/${file}";

  artifactsOf =
    package:
    map (entry: {
      kind = if isWheel entry.file then "wheel" else "sdist";
      package = package.name;
      inherit (package) version;
      filename = entry.file;
      drv = pkgs.fetchurl {
        url = urlOf package.name entry.file;
        inherit (entry) hash;
        name = entry.file;
      };
    }) (package.files or [ ]);

  planned = import ./planner.nix {
    inherit
      pkgs
      pydd
      installer
      markers
      noBinary
      python
      buildTools
      fromNixpkgs
      ;
    artifacts = lib.concatMap artifactsOf document.package;
    roots = [ ];
    venvName = environmentName;
    editables = lib.optionalAttrs packageMode {
      ${projectName} = {
        src = project;
        root = editableRoot;
        inherit mode;
      };
    };
    poetry = {
      lock = "${lockFile}";
      inherit groups extras;
    };
  };
in
planned
// {
  # The environment as a derivation, so that one command builds it.
  env = import ./application.nix {
    inherit pkgs;
    name = environmentName;
    application = planned.out;
    program = null;
  };
}

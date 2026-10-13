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
  # The pydd library. Its `writer` chooses the path; `./planner.nix` names both.
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
  # Whether a package built from source takes its native inputs from the
  # matching package of `python.pkgs`. `./hints.nix` gives the rule, and
  # `buildTools` replaces it per package.
  nixpkgsHints ? true,
  fromNixpkgs ? { },
  # Per-package node overrides. `./overrides.nix` gives the keys, and
  # `[tool.pydd.overrides.<package>]` declares the same; this argument wins.
  overrides ? { },
  # The escape hatch; `./planner.nix` gives the contract at `builders`.
  builders ? { },
  # Whether an evaluator in a planner may build to import, as in `./uv.nix`.
  allowImportFromDerivation ? false,
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

  # A package that the lock file can build from source: it has an sdist, or it
  # is the project itself.
  sourceBuilt =
    map (package: package.name) (
      lib.filter (package: lib.any (entry: !(isWheel entry.file)) (package.files or [ ])) document.package
    )
    ++ lib.optional packageMode projectName;
  hintedBuildTools = lib.optionalAttrs nixpkgsHints (
    import ./hints.nix { inherit lib pkgs python; } sourceBuilt
  );

  overridden = import ./overrides.nix {
    inherit lib pkgs project projectName;
    declared = pyproject.tool.pydd.overrides or { };
    inherit overrides;
  };

  planned = import ./planner.nix {
    inherit
      pkgs
      pydd
      installer
      markers
      noBinary
      python
      fromNixpkgs
      builders
      allowImportFromDerivation
      ;
    buildTools = hintedBuildTools // overridden.buildTools // buildTools;
    overrides = overridden.nodes;
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
# An ordinary derivation, as `./uv.nix` gives it (Lillecarl/pydd#7).
let
  root = import ./application.nix {
    inherit pkgs;
    name = environmentName;
    application = planned.out;
    passthru = builtins.removeAttrs planned [ "out" ] // {
      dynamic = planned.out;
      env = root;
    };
  };
in
root

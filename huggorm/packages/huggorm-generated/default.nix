{
  lib,
  python3Packages,
  # The Nix the surface describes (nixidae/nixidae#592).
  version,
  ruff,
  # The declarations, and the reader that turns them into the model.
  # A build input: pygen imports it and calls it instead of reflecting
  # on a compiled class.
  huggorm-gen,
  huggorm-decl,
  huggorm-dsl,
  ...
}:
python3Packages.buildPythonPackage {
  pname = "huggorm-generated";
  version = "0.1.0";
  pyproject = true;
  src = lib.fileset.toSource {
    root = ./.;
    fileset = lib.fileset.unions [
      ./LICENSE
      ./pyproject.toml
      ./setup.py
    ];
  };

  # setup.py imports huggorm_gen.pygen to run it.
  #
  # huggorm-bindings is not an input at all. The generator derives
  # every surface from the declarations (nixidae/nixidae#602), so this builds
  # beside the compile and not after it (nixidae/nixidae#717). The wrappers it
  # writes import the bindings at run time, so a consumer installs both:
  # `huggorm` propagates the two, and imports them in its check.
  build-system = [
    python3Packages.setuptools
    huggorm-gen
    huggorm-decl
    huggorm-dsl
    python3Packages.anyio
  ];

  # anyio because a generated wrapper hands back the async spelling of
  # a type when the bindings declare one: Store.real_path returns a
  # pathlib.Path in process and an anyio.Path from the wrapper, so the
  # emitted module imports anyio (nixidae/nixidae#577).
  propagatedBuildInputs = [ python3Packages.anyio ];
  # The metadata names huggorm-bindings, truthfully, and this build has
  # none. `huggorm` installs both and imports both in its check.
  dontCheckRuntimeDeps = true;

  # pygen lays its output out with `ruff format`.
  nativeBuildInputs = [ ruff ];

  env.HUGGORM_NIX_VERSION = version;

  # The emitted package and its stubs are typechecked as a pair by the
  # lane's `suite`, against this package installed in a venv.
}

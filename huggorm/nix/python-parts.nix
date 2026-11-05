/**
  huggorm's three pure-Python packages, for one interpreter's package set.
  A lane calls it with its own: the `-ft` lanes install these into a 3.14t
  venv, whose site-packages is `python3.14t`.

  `huggorm-dsl`
  : The LANGUAGE a declaration is written in, and the reader that parses
    one. No declaration and no emitter is in here, which is what lets the
    two below depend on it without depending on each other.

  `huggorm-decl`
  : The DECLARATIONS: one document per Nix class, and the lists saying
    which of them owns a compiled module. Data, plus the statement of what
    data there is.

  `huggorm-gen`
  : The EMITTERS, both backends in one package. `cppgen` fills
    huggorm_bindings, `pygen` fills huggorm_generated, and `payload` is the
    hand-written Python pygen copies into its output. One package because
    they read ONE IR from one reader. A boundary between them would say the
    split is architectural, and it is not.
*/
{
  lib,
  buildPythonPackage,
  setuptools,
}:
let
  # Its LICENSE, pyproject.toml and src, and no `tests/`: `generator-tests`
  # runs them, and a test edit must not rebuild every package downstream of
  # the generator.
  part =
    {
      name,
      imports,
      dependencies ? [ ],
    }:
    let
      dir = ../packages + "/${name}";
    in
    buildPythonPackage {
      pname = name;
      version = "0.1.0";
      pyproject = true;
      src = lib.fileset.toSource {
        root = dir;
        fileset = lib.fileset.unions [
          (dir + "/LICENSE")
          (dir + "/pyproject.toml")
          (dir + "/src")
        ];
      };
      build-system = [ setuptools ];
      inherit dependencies;
      pythonImportsCheck = [ imports ];
    };
in
lib.fix (self: {
  huggorm-dsl = part {
    name = "huggorm-dsl";
    imports = "huggorm_dsl";
  };
  huggorm-decl = part {
    name = "huggorm-decl";
    imports = "huggorm_decl";
    dependencies = [ self.huggorm-dsl ];
  };
  huggorm-gen = part {
    name = "huggorm-gen";
    imports = "huggorm_gen.cppgen";
    dependencies = [
      self.huggorm-dsl
      self.huggorm-decl
    ];
  };
})

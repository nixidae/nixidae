{
  lib,
  python3Packages,
  huggorm-bindings,
  huggorm-generated,
  huggorm-gen,
  huggorm-decl,
  huggorm-dsl,
  tree-sitter-nix,
  ruff,
  ...
}:
# The suite runs against this package in `suite.nix`, not in a check phase
# here, where only PYTHONPATH reaches the package (nixidae/nixidae#1046).
python3Packages.buildPythonPackage {
  pname = "huggorm";
  version = "0.1.0";
  pyproject = true;
  src = lib.fileset.toSource {
    root = ./.;
    fileset = lib.fileset.unions [
      ./LICENSE
      ./pyproject.toml
      ./setup.py
      ./huggorm
    ];
  };

  # The generator too, because `setup.py` writes `huggorm/__init__.py`
  # before setuptools resolves the package list. The front door is a
  # mapping of Python names onto the two packages behind it, so it is
  # derived rather than tracked by hand (nixidae/nixidae#601).
  #
  # ruff lays the front door out, as it does the generated package.
  nativeBuildInputs = [ ruff ];

  build-system = with python3Packages; [
    setuptools
    huggorm-gen
    huggorm-decl
    huggorm-dsl
  ];

  # The front door reads the declarations for the Nix the bindings link
  # (nixidae/nixidae#592).
  env.HUGGORM_NIX_VERSION = huggorm-bindings.nixVersion;
  env.HUGGORM_NIX_GC = if huggorm-bindings.hasCollector then "1" else "0";

  # generated is propagated so anyone writing code in/downstream of
  # huggorm sees huggorm_generated in their environment.
  # watchdog: the OS telling the watcher a file moved, instead of the
  # watcher stat-ing for it - inotify on Linux, FSEvents on macOS.
  # `huggorm/notify.py` says why it replaced asyncinotify.
  # msgpack: the value format of the Unix socket transport (nixidae/nixidae#714).
  # typing-extensions: `TypeAliasType`, for `JsonValue` on 3.11.
  propagatedBuildInputs = [
    huggorm-bindings
    huggorm-generated
    python3Packages.watchdog
    python3Packages.msgpack
    python3Packages.typing-extensions
  ];

  # `huggorm.fod` reads the hash binding out of Nix source.
  optional-dependencies.fod = [
    python3Packages.tree-sitter
    tree-sitter-nix
  ];
  # `huggorm.ssh` runs a server over an SSH session.
  optional-dependencies.ssh = [ python3Packages.asyncssh ];
}

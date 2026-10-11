{
  # huggorm brings the nixpkgs and the Python every package here builds
  # on, so the bindings and havsorm share one interpreter.
  huggorm ? import ../huggorm { },
}:
let
  inherit (huggorm) pkgs;
  inherit (pkgs) lib;
  python3 = huggorm.python;
  # nixpkgs builds Salt as an application. The tests import it.
  salt = python3.pkgs.toPythonModule pkgs.salt;
in
rec {
  inherit huggorm pkgs salt;

  # nix build --file . havsorm
  #
  # Lints, then runs the suite: the loader finds the renderer through
  # the installed entry point, and `salt-call --local` renders an SLS.
  havsorm = python3.pkgs.buildPythonPackage {
    pname = "havsorm";
    version = "0.1.0";
    pyproject = true;
    src = lib.fileset.toSource {
      root = ./.;
      fileset = lib.fileset.unions [
        ./pyproject.toml
        ./README.md
        ./LICENSE
        ./src
        ./tests
      ];
    };
    build-system = [ python3.pkgs.setuptools ];
    dependencies = [ huggorm.huggorm ];
    nativeCheckInputs = [
      python3.pkgs.pytestCheckHook
      salt
      pkgs.ruff
    ];
    preCheck = ''
      ruff check --no-cache src tests
    '';
    # Not the renderer: it imports Salt, which runs havsorm and is not
    # one of its dependencies.
    pythonImportsCheck = [ "havsorm" ];
  };

  # nix run --file . python -- -m pytest
  python = python3.withPackages (_: [
    havsorm
    salt
    python3.pkgs.pytest
  ]);
}

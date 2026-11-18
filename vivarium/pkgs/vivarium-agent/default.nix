{
  lib,
  python3,
  python3Packages,
  runCommand,
  dinit-client,
}:
let
  agent = python3Packages.buildPythonPackage {
    pname = "vivarium-agent";
    version = "0.1.0";

    # Every guest's closure holds this package, so its source is only what
    # the guest runs: a test edit must not rebuild every guest.
    src = lib.fileset.toSource {
      root = ./.;
      fileset = lib.fileset.unions [
        ./pyproject.toml
        ./vivarium_agent
      ];
    };

    pyproject = true;

    build-system = [ python3Packages.hatchling ];
    dependencies = [
      python3Packages.rpyc
      python3Packages.anyio
      # The agent's init on a dinix guest. Only the agent's dinit calls use it.
      dinit-client
    ];

    pythonImportsCheck = [ "vivarium_agent.agent" ];

    passthru = { inherit tests; };

    meta = {
      description = "The guest half of vivarium's control channel";
      license = lib.licenses.asl20;
      platforms = lib.platforms.linux ++ lib.platforms.darwin;
      mainProgram = "vivarium-agent";
    };
  };

  # Outside the package, for the reason `src` gives.
  tests =
    runCommand "vivarium-agent-tests"
      {
        nativeBuildInputs = [
          (python3.withPackages (_: [
            agent
            python3Packages.pytest
          ]))
        ];
      }
      ''
        python -m pytest -p no:cacheprovider ${
          lib.fileset.toSource {
            root = ./.;
            fileset = lib.fileset.fileFilter (file: file.hasExt "py") ./tests;
          }
        }/tests
        touch $out
      '';
in
agent

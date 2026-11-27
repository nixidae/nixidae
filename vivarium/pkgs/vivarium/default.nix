{
  lib,
  nss,
  python3Packages,
  vivarium-agent,
  vivarium-ca,
  vivarium-runner,
  vivarium-socks,
}:

python3Packages.buildPythonPackage {
  pname = "vivarium";
  version = "0.1.0";

  src = lib.fileset.toSource {
    root = ./.;
    fileset = lib.fileset.unions [
      ./pyproject.toml
      ./vivarium
      ./tests
    ];
  };

  pyproject = true;

  build-system = [ python3Packages.hatchling ];
  dependencies = [
    # The mechanism: guests, backends, the agent channel. This package
    # owns the sequence and none of that.
    vivarium-runner
    # The control channel's protocol: remote.py serves it to a run's clients.
    vivarium-agent
    python3Packages.anyio
    # The spec is input, so it is validated rather than read
    # defensively at each point of use. See vivarium/spec.py.
    python3Packages.pydantic
    # A phase may be a pytest run. See vivarium/pytest_plugin.py.
    python3Packages.pytest
    # A test in a guest measures itself for the host's pytest-cov. See
    # vivarium/pytest_worker.py.
    python3Packages.coverage
    # The run's lab CA (vivarium/trust.py): the certificates, where the
    # user's root lives, and the public roots when the host names none.
    vivarium-ca
    python3Packages.platformdirs
    python3Packages.certifi
    # The host's split proxy into the lab, for `vivarium shell`. See
    # vivarium/session.py.
    vivarium-socks
  ];

  pythonImportsCheck = [ "vivarium" ];

  # The phase logic is pure, so it is checked here rather than by booting
  # a guest to find out what a failure skips. `vivarium/tests/` holds only
  # what needs no machine; anything that needs one is a test in `tests/`
  # at the root, where a guest is available.
  nativeCheckInputs = [
    python3Packages.pytestCheckHook
    # tests/test_browser.py puts a lab root in an NSS database.
    nss.tools
  ];

  meta = {
    description = "Drive a run of NixOS guests a step at a time";
    license = lib.licenses.asl20;
    platforms = lib.platforms.linux ++ lib.platforms.darwin;
    mainProgram = "vivarium-runner";
  };
}

{
  lib,
  python3Packages,
  # The fork nixidae pins (nanopynix/nix/kr8s.nix).
  kr8s,
}:

python3Packages.buildPythonPackage {
  pname = "vivarium-lb";
  version = "0.1.0";

  src = lib.fileset.toSource {
    root = ./.;
    fileset = lib.fileset.unions [
      ./pyproject.toml
      ./vivarium_lb
      ./tests
    ];
  };

  pyproject = true;

  build-system = [ python3Packages.hatchling ];
  dependencies = [
    python3Packages.anyio
    python3Packages.httpx
    kr8s
  ];

  nativeCheckInputs = [ python3Packages.pytestCheckHook ];
  pythonImportsCheck = [ "vivarium_lb" ];

  meta = {
    description = "LoadBalancer Services for a Kubernetes cluster in a vivarium lab";
    license = lib.licenses.asl20;
    mainProgram = "vivarium-lb";
    platforms = lib.platforms.linux;
  };
}

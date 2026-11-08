{
  lib,
  python3Packages,
  vivarium-runner,
}:

python3Packages.buildPythonPackage {
  pname = "vivarium-k8s";
  version = "0.1.0";

  src = lib.fileset.toSource {
    root = ./.;
    fileset = lib.fileset.unions [
      ./pyproject.toml
      ./vivarium_k8s
    ];
  };

  pyproject = true;

  build-system = [ python3Packages.hatchling ];
  dependencies = [
    vivarium-runner
    python3Packages.anyio
  ];

  pythonImportsCheck = [ "vivarium_k8s" ];

  meta = {
    description = "Bring a kubeadm cluster up on vivarium guests";
    license = lib.licenses.asl20;
    platforms = lib.platforms.linux;
  };
}

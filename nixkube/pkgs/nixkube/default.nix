# SPDX-License-Identifier: MIT

{
  buildPythonApplication, # Builder
  hatchling, # Build system
  coreutils, # ln
  cryptography, # ssh-keygen Python
  cri-proto-python, # CRI gRPC bindings
  csi-proto-python, # CSI gRPC bindings
  nri-proto-python, # NRI ttRPC bindings
  grpclib-nri, # NRI protocol utilities
  googleapis-common-protos, # Google Errors
  gitMinimal, # Lix requires Git since it doesn't use libgit2
  kr8s, # Kubernetes API
  shellous, # subprocessing
  lruLix, # We need a Nix implementation.... :)
  nix_init_db, # Import from one nix DB to another
  openssh, # Copying to cache
  rsync, # hardlinking
  util-linuxMinimal, # mount, umount
  pyzmq, # Talking to OCI hooks
  aiofiles, # Async file I/O
  nri-wait, # OCI hook for waiting on NRI builds
  pytest, # Unit tests
  pytest-asyncio, # Async test support
}:
let
  pyproject = builtins.fromTOML (builtins.readFile ./pyproject.toml);
in
buildPythonApplication {
  pname = pyproject.project.name;
  version = pyproject.project.version;
  src = ./.;
  pyproject = true;
  build-system = [ hatchling ];
  dependencies = [
    coreutils
    cryptography
    cri-proto-python
    csi-proto-python
    nri-proto-python
    grpclib-nri
    googleapis-common-protos
    gitMinimal
    kr8s
    shellous
    lruLix
    nix_init_db
    openssh
    rsync
    util-linuxMinimal
    pyzmq
    aiofiles
    nri-wait
  ];
  nativeCheckInputs = [
    pytest
    pytest-asyncio
  ];
  meta.mainProgram = "nixkube";
}

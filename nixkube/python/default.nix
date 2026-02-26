# SPDX-License-Identifier: MIT

{
  buildPythonApplication, # Builder
  hatchling, # Build system
  coreutils, # ln
  cryptography, # ssh-keygen Python
  cri-proto-python, # CRI gRPC bindings
  csi-proto-python, # CSI gRPC bindings
  nri-proto-python, # NRI ttRPC bindings
  grpclib-ttrpc, # ttRPC server/client over grpclib primitives
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
  nri-wait, # OCI hook for waiting on NRI builds
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
    grpclib-ttrpc
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
    nri-wait
  ];
  meta.mainProgram = "nix-csi";
}

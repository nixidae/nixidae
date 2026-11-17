# Build it with a static toolchain (`pkgsStatic.callPackage`): it runs as the
# kernel's `init=`, before the Nix store, and so its loader, is mounted.
{
  lib,
  rustPlatform,
}:

rustPlatform.buildRustPackage {
  pname = "dinix-boot";
  version = "0.1.0";

  src = lib.fileset.toSource {
    root = ../.;
    fileset = lib.fileset.unions [
      ./Cargo.toml
      ./Cargo.lock
      ./src
      # Shared with dinix-init, so the two expand variables alike.
      ../dinix-init/src/expand.rs
    ];
  };
  sourceRoot = "source/dinix-boot";

  # No dependencies, so nothing to fetch and no hash to keep in step.
  cargoLock.lockFile = ./Cargo.lock;

  # As dinix-init: Cargo's `strip = true` does not survive buildRustPackage.
  stripAllList = [ "bin" ];

  meta = {
    description = "A VM's first process: mounts what dinit needs from a spec, then execs dinit";
    mainProgram = "dinix-boot";
    license = lib.licenses.asl20;
  };
}

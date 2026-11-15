/*
  A vhost-user virtio-fs server for macOS hosts, for QEMU's
  vhost-user-fs-pci. virtiofsd is Linux-only (O_PATH, /proc/self/fd, epoll).

  The FUSE side is libkrun's macOS passthrough (containers/libkrun
  a0375321bb1e, Apache-2.0; crosvm's files keep their BSD headers), plus
  indirect descriptors, which a macOS guest's driver uses for every request.
  The allowlist (`--allowlist`) is github.com/nixidae/libkrun's, which pynixd's
  Linux build VM uses. nixidae/nixidae#1168.
*/
{
  lib,
  rustPlatform,
}:
rustPlatform.buildRustPackage {
  pname = "darwin-virtiofsd";
  version = "0.1.0";

  src = lib.fileset.toSource {
    root = ./.;
    fileset = lib.fileset.unions [
      ./Cargo.toml
      ./Cargo.lock
      ./src
    ];
  };

  cargoLock.lockFile = ./Cargo.lock;

  meta = {
    description = "vhost-user virtio-fs server for macOS hosts";
    license = with lib.licenses; [
      asl20
      bsd3
    ];
    platforms = lib.platforms.darwin;
    mainProgram = "darwin-virtiofsd";
  };
}

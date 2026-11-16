/*
  The router's two halves (docs/design/router-lab.md): `relay` runs in the
  router guest, `egress` on the host. `vhostUser` adds the vsock device a
  UML router needs; the relay builds without it.
*/
{
  lib,
  rustPlatform,
  vhostUser ? false,
}:
rustPlatform.buildRustPackage {
  pname = "vivarium-egress";
  version = "0.0.0";
  src = lib.fileset.toSource {
    root = ./.;
    fileset = lib.fileset.unions [
      ./Cargo.toml
      ./Cargo.lock
      ./src
    ];
  };
  cargoLock.lockFile = ./Cargo.lock;
  buildFeatures = lib.optional vhostUser "vhost-user";
  meta = {
    description = "The vivarium router's relay and the host's egress";
    license = lib.licenses.asl20;
  };
}

/*
  The router's two halves (docs/design/router-lab.md): `relay` runs in the
  router guest, `egress` on the host. `vhostUser` adds the vsock device a
  UML router needs; the relay builds without it. On macOS only the egress
  builds: the relay is Linux's (tproxy, SO_ORIGINAL_DST).
*/
{
  lib,
  stdenv,
  rustPlatform,
  vhostUser ? false,
}:
let
  bins = lib.optionals stdenv.hostPlatform.isDarwin [
    "--bin"
    "egress"
  ];
in
rustPlatform.buildRustPackage {
  cargoBuildFlags = bins;
  cargoTestFlags = bins;
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
  # The tests bind 127.0.0.1, which the darwin sandbox refuses.
  __darwinAllowLocalNetworking = true;
  buildFeatures = lib.optional vhostUser "vhost-user";
  meta = {
    description = "The vivarium router's relay and the host's egress";
    license = lib.licenses.asl20;
  };
}

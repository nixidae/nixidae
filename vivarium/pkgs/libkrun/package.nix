/*
  libkrun 2.0 as the krun backend runs it: the library and its C header,
  with the virtio-net and virtio-blk devices, and no init blob, because a
  vivarium guest boots its own kernel, initrd and init.

  The source is the `nixidae` branch of nixidae/libkrun: unreleased
  libkrun main with vivarium's changes on top, each one commit
  (nixidae/nixidae#1077, nixidae/nixidae#1163). They let the host's store
  be an overlayfs lower layer in the guest from inside a user namespace,
  align the initrd, show the guest only an allowlist of the store (on a
  writable share too, for pynixd's build VM), cache a lookup that finds
  nothing for a host-set time, skip open, flush and readlink round trips on
  an unchanging read-only share, and on macOS add TSO, Rosetta
  and a read-only DAX window.

  The branch carries `include/libkrun.h` regenerated after each API change,
  so a bump needs no generator here.
*/
{
  lib,
  stdenv,
  fetchFromGitHub,
  rustPlatform,
  cargo,
  rustc,
  pkg-config,
}:
stdenv.mkDerivation (finalAttrs: {
  pname = "libkrun";
  version = "2.0.0-unstable-2026-11-03";

  src = fetchFromGitHub {
    owner = "nixidae";
    repo = "libkrun";
    rev = "28fc6bc83edf93706593cbdf8b46efcddce6f3ca";
    hash = "sha256-irxRkNhpNMxOJ/iv7KAGcrzvQEWMygFnmuWIC2a7ifs=";
  };

  cargoDeps = rustPlatform.fetchCargoVendor {
    inherit (finalAttrs) src;
    hash = "sha256-QjkNQzntusdlK37ka47B7BDfbPqc6Za8VGKLYku7jYE=";
  };

  nativeBuildInputs = [
    rustPlatform.cargoSetupHook
    cargo
    rustc
    pkg-config
  ];

  buildPhase = ''
    runHook preBuild
    cargo build --release --offline -p libkrun --features ffi,net,blk
    runHook postBuild
  '';

  installPhase = ''
    runHook preInstall
    install -D target/release/libkrun.so $out/lib/libkrun.so.2
    ln -s libkrun.so.2 $out/lib/libkrun.so
    install -D -m 444 include/libkrun.h $out/include/libkrun.h
    runHook postInstall
  '';

  meta = {
    description = "libkrun 2.0, for vivarium's krun backend";
    homepage = "https://github.com/nixidae/libkrun";
    license = lib.licenses.asl20;
    platforms = lib.platforms.linux;
  };
})

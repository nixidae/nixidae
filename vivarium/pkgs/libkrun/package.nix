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
  an unchanging read-only share, and on macOS add TSO, Rosetta, a
  read-only DAX window and 16-bit MMIO writes (Linux's PL011 driver makes
  them). Upstream's 925c003e is picked too: without "arm,primecell" an
  aarch64 guest gets no ttyAMA0.

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
  zstd,
  fixDarwinDylibNames,
}:
let
  lib' = "libkrun${stdenv.hostPlatform.extensions.sharedLibrary}";
  versioned = if stdenv.hostPlatform.isDarwin then "libkrun.2.dylib" else "libkrun.so.2";
in
stdenv.mkDerivation (finalAttrs: {
  pname = "libkrun";
  version = "2.0.0-unstable-2026-11-03";

  src = fetchFromGitHub {
    owner = "nixidae";
    repo = "libkrun";
    rev = "fb452acd8821e97b63ceae8240392c50f65be30a";
    hash = "sha256-l8KYUeQ+T86Br0YMK+1ZRoTetHcgTuT8JxGBQYnjfhs=";
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
  ]
  ++ lib.optional stdenv.hostPlatform.isDarwin fixDarwinDylibNames;

  # zstd-sys links the system libzstd, which it finds through pkg-config.
  buildInputs = [ zstd ];

  buildPhase = ''
    runHook preBuild
    cargo build --release --offline -p libkrun --features ffi,net,blk
    runHook postBuild
  '';

  installPhase = ''
    runHook preInstall
    install -D target/release/${lib'} $out/lib/${versioned}
    ln -s ${versioned} $out/lib/${lib'}
    install -D -m 444 include/libkrun.h $out/include/libkrun.h
    runHook postInstall
  '';

  meta = {
    description = "libkrun 2.0, for vivarium's krun backend";
    homepage = "https://github.com/nixidae/libkrun";
    license = lib.licenses.asl20;
    platforms = lib.platforms.linux ++ [ "aarch64-darwin" ];
  };
})

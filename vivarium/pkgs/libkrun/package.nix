/*
  libkrun 2.0 as the krun backend runs it: the library and its C header,
  with the virtio-net and virtio-blk devices, and no init blob, because a
  vivarium guest boots its own kernel, initrd and init.

  2.0 is unreleased: this is a pinned commit of main. 2.0 replaces 1.x's
  C API with a builder API, which `krun-launch` drives, and adds x86 ACPI
  (nixidae/nixidae#1077, nixidae/nixidae#1163).

  The patches let the host's store be an overlayfs lower layer in the
  guest from inside a user namespace, and align the initrd. 1.x's fourth
  patch, the Always cache policy for a read-only share, is
  `krun_fs_device_set_cache_policy` in 2.0.
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
  version = "2.0.0-unstable-2026-10-30";

  src = fetchFromGitHub {
    owner = "libkrun";
    repo = "libkrun";
    rev = "a0375321bb1e6e51312e022af53e9e7668d3a07e";
    hash = "sha256-Hq/zUSLtCXqDDYXBep4D1seb/DTHnZ9cH4wh2B1eacI=";
  };

  patches = [
    ./0001-virtio-fs-never-announce-submounts.patch
    ./0002-virtio-fs-retry-an-open-without-O_NOATIME.patch
    ./0004-x86_64-load-the-initrd-at-a-page-aligned-address.patch
  ];

  cargoDeps = rustPlatform.fetchCargoVendor {
    inherit (finalAttrs) src patches;
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
    homepage = "https://github.com/libkrun/libkrun";
    license = lib.licenses.asl20;
    platforms = lib.platforms.linux;
  };
})

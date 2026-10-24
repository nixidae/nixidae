/*
  libkrun as the krun backend runs it: a virtio-net device for passt and
  the segments, a virtio-blk device for the root disk, and the patches
  that let the host's store be an overlayfs lower layer in the guest from
  inside a user namespace and keep the guest's cache of it.

  The latest 1.x release, ahead of nixpkgs. libkrun 2.0 replaces the C API
  that krun_launch.py drives, so it waits for its release
  (nixidae/nixidae#1077).
*/
{
  libkrun,
  fetchFromGitHub,
  rustPlatform,
}:
(libkrun.override {
  withNet = true;
  withBlk = true;
}).overrideAttrs
  (
    finalAttrs: old: {
      version = "1.19.7";
      src = fetchFromGitHub {
        owner = "libkrun";
        repo = "libkrun";
        tag = "v${finalAttrs.version}";
        hash = "sha256-VtepY0sAqP18OIhIPwESoGi7E2Ahs4ihJjLyLH78JAM=";
      };
      cargoDeps = rustPlatform.fetchCargoVendor {
        inherit (finalAttrs) src;
        hash = "sha256-Ab7b37Npf0vNg7rF8KfPdLYCBuBCyRAYhR13RLecV04=";
      };
      patches = (old.patches or [ ]) ++ [
        ./0001-virtio-fs-never-announce-submounts.patch
        ./0002-virtio-fs-retry-an-open-without-O_NOATIME.patch
        ./0003-virtio-fs-keep-the-guest-cache-of-a-read-only-share.patch
        ./0004-x86_64-load-the-initrd-at-a-page-aligned-address.patch
      ];
    }
  )

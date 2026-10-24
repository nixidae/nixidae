/*
  libkrun as the krun backend runs it: a virtio-net device for passt and
  the segments, a virtio-blk device for the root disk, and the patches
  that let the host's store be an overlayfs lower layer in the guest from
  inside a user namespace and keep the guest's cache of it.
*/
{ libkrun }:
(libkrun.override {
  withNet = true;
  withBlk = true;
}).overrideAttrs
  (old: {
    patches = (old.patches or [ ]) ++ [
      ./0001-virtio-fs-never-announce-submounts.patch
      ./0002-virtio-fs-retry-an-open-without-O_NOATIME.patch
      ./0003-virtio-fs-keep-the-guest-cache-of-a-read-only-share.patch
      ./0004-x86_64-load-the-initrd-at-a-page-aligned-address.patch
    ];
  })

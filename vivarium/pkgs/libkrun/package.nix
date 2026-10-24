/*
  libkrun as the krun backend runs it: a virtio-net device for passt and
  the segments, a virtio-blk device for the root disk, and the patches
  that let the host's store be an overlayfs lower layer in the guest from
  inside a user namespace.
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
    ];
  })

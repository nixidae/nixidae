/*
  libkrun as the krun backend runs it: a virtio-net device for passt and
  the segments, a virtio-blk device for the root disk, and the patch that
  lets the host's /nix be an overlayfs lower layer in the guest.
*/
{ libkrun }:
(libkrun.override {
  withNet = true;
  withBlk = true;
}).overrideAttrs
  (old: {
    patches = (old.patches or [ ]) ++ [ ./0001-virtio-fs-never-announce-submounts.patch ];
  })

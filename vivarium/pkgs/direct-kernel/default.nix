# A guest kernel booted with no initrd, for a dinix guest
# (docs/design/dinix-guests.md): what runs before the root disk and the
# store are mounted is built in, not a module. The rest stays a module,
# which a guest loads from the store once it is mounted.
#
# `kernel` is the guest's own (`boot.kernelPackages.kernel`), so a dinix
# guest runs the version a NixOS guest does.
{ lib, kernel }:
kernel.override (old: {
  structuredExtraConfig =
    (old.structuredExtraConfig or { })
    // (with lib.kernel; {
      # The devices: QEMU's are PCI, libkrun's MMIO.
      VIRTIO = yes;
      VIRTIO_PCI = yes;
      VIRTIO_MMIO = yes;
      VIRTIO_MMIO_CMDLINE_DEVICES = yes;
      # The root disk, and the agent's port.
      VIRTIO_BLK = yes;
      EXT4_FS = yes;
      VIRTIO_CONSOLE = yes;
      # The store: virtiofs with DAX on krun, under an overlay.
      FUSE_FS = yes;
      VIRTIO_FS = yes;
      FUSE_DAX = yes;
      OVERLAY_FS = yes;
    });
  # fuse is built in, so pkgs/fuse-dax's two fixes go into the kernel.
  kernelPatches = (old.kernelPatches or [ ]) ++ [
    {
      name = "fuse-do-not-invalidate-a-DAX-inode-s-mappings-on-open";
      patch = ../fuse-dax/0001-fuse-do-not-invalidate-a-DAX-inode-s-mappings-on-open.patch;
    }
    {
      name = "fuse-unmap-again-while-waiting-to-reclaim-a-DAX-range";
      patch = ../fuse-dax/0002-fuse-unmap-again-while-waiting-to-reclaim-a-DAX-range.patch;
    }
  ];
})

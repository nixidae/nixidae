# The host programs the backends start, as the paths each backend's module
# writes to `vivarium.launch.config`. One file, so two backends that start
# the same program name the same store path.
#
# Each value is a thunk until a module reads it: a guest names only its own
# backend's tools, and naming a path is what makes Nix build it.
{ pkgs, lib }:
let
  qemu = pkgs.qemu_kvm;
in
{
  passt = "${pkgs.callPackage ../pkgs/passt { }}/bin/passt";
  qemu = "${qemu}/bin/qemu-system-x86_64";
  qemuImg = "${qemu}/bin/qemu-img";
  virtiofsd = "${pkgs.virtiofsd}/bin/virtiofsd";
  gvproxy = lib.getExe' pkgs.gvproxy "gvproxy";
  krun = lib.getExe (
    pkgs.callPackage ../pkgs/krun-launch/package.nix {
      libkrun = pkgs.callPackage ../pkgs/libkrun/package.nix { };
    }
  );
  crun = lib.getExe pkgs.crun;
  setpriv = "${lib.getBin pkgs.util-linux}/bin/setpriv";
  # nixos-test's OCR packages, for the same reading of the same screens.
  tesseract = "${pkgs.tesseract4}/bin/tesseract";
  magick = "${pkgs.imagemagick_light}/bin/magick";
}

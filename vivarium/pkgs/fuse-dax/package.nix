# The guest kernel's fuse and virtiofs modules, rebuilt with two DAX fixes.
#
# Since Linux 6.15 a DAX page that a process maps holds a reference, and
# fuse was not changed to match. On an open without FOPEN_KEEP_CACHE it
# drops the DAX entries of pages still mapped, and the next fault on one
# gets SIGBUS (smol-machines/smolvm#1741, nixidae/nixidae#1077). Range
# reclaim can also wait forever for a page that a fault maps again.
#
# Only fs/fuse is built, against the stock kernel, so the kernel itself
# still comes from the binary cache. depmod prefers `updates/` over the
# kernel's own copy of a module.
{
  lib,
  stdenv,
  kernel,
  kernelModuleMakeFlags,
}:
stdenv.mkDerivation {
  pname = "fuse-dax";
  inherit (kernel) version src;

  sourceRoot = "fuse";
  unpackPhase = ''
    runHook preUnpack
    mkdir fuse
    tar -xf $src --wildcards --strip-components=3 --directory fuse '*/fs/fuse/*'
    runHook postUnpack
  '';

  patches = [
    ./0001-fuse-do-not-invalidate-a-DAX-inode-s-mappings-on-open.patch
    ./0002-fuse-unmap-again-while-waiting-to-reclaim-a-DAX-range.patch
  ];
  patchFlags = [ "-p3" ];

  nativeBuildInputs = kernel.moduleBuildDependencies;
  hardeningDisable = [ "pic" ];

  makeFlags = kernelModuleMakeFlags ++ [
    "-C"
    "${kernel.dev}/lib/modules/${kernel.modDirVersion}/build"
  ];
  preBuild = ''
    makeFlagsArray+=("M=$PWD")
  '';
  buildFlags = [ "modules" ];

  installPhase = ''
    runHook preInstall
    install -D --mode=0444 --target-directory=$out/lib/modules/${kernel.modDirVersion}/updates *.ko
    runHook postInstall
  '';

  meta.license = lib.licenses.gpl2Only;
}

# The kernel image libkrun loads: an ELF vmlinux on x86_64, which it takes
# instead of a bzImage, and the raw Image on aarch64 (`load_external_kernel`).
# The dev output's vmlinux is 433 MB of debug info; stripped, the run's
# closure holds only this.
{
  lib,
  stdenv,
  runCommand,
  binutils,
  kernel,
}:
# The image's file name is the kernel's own `target`: nixpkgs 26.11 took it
# out of the platform (`linux-kernel`), and reading it there fails.
if stdenv.hostPlatform.isAarch64 then
  "${kernel}/${kernel.target}"
else
  "${runCommand "vmlinux-${kernel.version}" { nativeBuildInputs = [ binutils ]; } ''
    objcopy --strip-all ${lib.getDev kernel}/vmlinux $out
  ''}"

/*
  `vivarium-krun`, the process a krun guest runs as: a small C program over
  libkrun 2.0's builder API. The runner builds its command line from the
  guest's spec (`vivarium_runner.backend.Krun`).

  On macOS the binary carries the hypervisor entitlement, signed ad hoc.
*/
{
  lib,
  stdenv,
  libkrun,
  darwin,
}:
stdenv.mkDerivation {
  pname = "vivarium-krun";
  version = "0.1.0";
  src = ./vivarium-krun.c;
  dontUnpack = true;
  nativeBuildInputs = lib.optional stdenv.hostPlatform.isDarwin darwin.sigtool;
  buildInputs = [ libkrun ];
  buildPhase = ''
    runHook preBuild
    $CC -std=gnu11 -O2 -Wall -Wextra -Werror -o vivarium-krun $src -lkrun
    runHook postBuild
  '';
  installPhase = ''
    runHook preInstall
    install -D vivarium-krun $out/bin/vivarium-krun
    ${lib.optionalString stdenv.hostPlatform.isDarwin ''
      codesign --entitlements ${./hypervisor.entitlements} -f -s - $out/bin/vivarium-krun
    ''}
    runHook postInstall
  '';
  # Stripping drops the signature, and with it the entitlement.
  dontStrip = stdenv.hostPlatform.isDarwin;
  meta = {
    description = "Runs one vivarium guest under libkrun";
    mainProgram = "vivarium-krun";
    license = lib.licenses.asl20;
    platforms = lib.platforms.linux ++ [ "aarch64-darwin" ];
  };
}

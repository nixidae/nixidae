/*
  `vivarium-krun`, the process a krun guest runs as: a small C program over
  libkrun 2.0's builder API. The runner builds its command line from the
  guest's spec (`vivarium_runner.backend.Krun`).
*/
{
  lib,
  stdenv,
  libkrun,
}:
stdenv.mkDerivation {
  pname = "vivarium-krun";
  version = "0.1.0";
  src = ./vivarium-krun.c;
  dontUnpack = true;
  buildInputs = [ libkrun ];
  buildPhase = ''
    runHook preBuild
    $CC -std=gnu11 -O2 -Wall -Wextra -Werror -o vivarium-krun $src -lkrun
    runHook postBuild
  '';
  installPhase = ''
    runHook preInstall
    install -D vivarium-krun $out/bin/vivarium-krun
    runHook postInstall
  '';
  meta = {
    description = "Runs one vivarium guest under libkrun";
    mainProgram = "vivarium-krun";
    license = lib.licenses.asl20;
    platforms = lib.platforms.linux;
  };
}

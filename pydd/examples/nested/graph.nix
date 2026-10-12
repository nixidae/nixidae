{
  system,
  bash,
  inner,
}:
derivation {
  name = "nested";
  inherit system;
  builder = "${bash}/bin/sh";
  args = [
    "-c"
    "read -r line < ${builtins.outputOf (builtins.outputOf inner.drv "out") "out"}; echo \"outer read: $line\" > $out"
  ];
  __contentAddressed = true;
  outputHashMode = "nar";
  outputHashAlgo = "sha256";
}

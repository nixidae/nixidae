# What a node reads of a nixpkgs package, for every ecosystem's hints.
#
# A node runs no stdenv, so no setup hook follows `.dev` to `.out` for it,
# and a hook is no input at all.
{ lib }:
rec {
  isHook = drv: lib.hasSuffix "-hook" (lib.getName drv) || lib.hasSuffix "-hook.sh" (lib.getName drv);

  # **An input of nixpkgs is the output stdenv chose.** `buildInputs` of
  # pyyaml holds `libyaml.dev`, and `lib.getLib` returns a chosen output as it
  # is, so pyyaml got no `-L` for `libyaml.so` and fell back to pure Python.
  # This reads the outputs of the package, whatever output the input names.
  outputOf =
    names: drv:
    let
      found = builtins.filter (name: drv ? ${name}) names;
    in
    if found == [ ] then drv else drv.${builtins.head found};

  # Each output that a build reads: headers, libraries and programs.
  outputsOf =
    drv:
    lib.unique [
      (outputOf [ "dev" "out" ] drv)
      (outputOf [ "lib" "out" ] drv)
      (outputOf [ "bin" "out" ] drv)
    ];
}

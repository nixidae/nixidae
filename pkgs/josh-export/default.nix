# Exports each source to its own repository. `josh_export.py` says how.
#
# josh comes from the nixpkgs flake.lock pins: another version of josh may
# filter the same history to other hashes, and every export after that
# would be refused. It runs the caller's nix.
{
  lib,
  writers,
  git,
  josh,
}:
writers.writePython3Bin "josh-export" {
  makeWrapperArgs = [
    "--prefix"
    "PATH"
    ":"
    (lib.makeBinPath [
      git
      josh
    ])
  ];
  # Lines run to 120 characters.
  flakeIgnore = [ "E501" ];
} ./josh_export.py

# The tool configuration every Python source shares, copied into each one.
# nixidae/nixidae#1104.
#
# The originals live in `nix/shared/`. Each source gets a byte-for-byte copy
# at `<source>/nix/shared/<file>`, because josh exports a source alone and a
# sandboxed check sees only the source's own store path.
{
  lib,
  pkgs,
  sources,
}:
let
  files = [
    "ruff.toml"
    "ruff-anyio.toml"
  ];

  original = file: ./shared + "/${file}";

  # A `<source>/nix/shared/<file>` for each pair.
  copies = lib.concatMap (source: map (file: { inherit source file; }) files) sources;
in
{
  # Writes every copy.
  update = pkgs.writeShellScriptBin "shared-config-update" ''
    set -euo pipefail
    # A secondary jj workspace has no .git. nixidae/nixidae#18.
    if ! root=$(${lib.getExe pkgs.jujutsu} root 2>/dev/null) &&
      ! root=$(${lib.getExe pkgs.git} rev-parse --show-toplevel 2>/dev/null); then
      echo "shared-config-update: $PWD is in no jj workspace and no git checkout" >&2
      exit 1
    fi
    cd "$root"
    ${lib.concatMapStrings (
      { source, file }:
      ''
        mkdir -p ${source}/nix/shared
        cp --no-preserve=mode ${original file} ${source}/nix/shared/${file}
      ''
    ) copies}
  '';

  # Is every copy the same bytes as its original?
  check = pkgs.runCommand "shared-config-in-step" { } ''
    ${lib.concatMapStrings (
      { source, file }:
      let
        copy = ../. + "/${source}/nix/shared/${file}";
      in
      ''
        if ! ${if builtins.pathExists copy then "cmp --quiet ${original file} ${copy}" else "false"}; then
          echo "${source}/nix/shared/${file} differs from nix/shared/${file}. Run: nix run --file . shared-config-update" >&2
          exit 1
        fi
      ''
    ) copies}
    touch $out
  '';
}

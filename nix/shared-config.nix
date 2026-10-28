# The files every source shares, copied into each one. nixidae/nixidae#1104.
#
# The originals live in `nix/shared/`. josh exports a source alone and a
# sandboxed check sees only the source's own store path, so each source gets
# its own copy:
#
# - the tool configuration, byte for byte, at `<source>/nix/shared/<file>`;
# - `<source>/nix/sources.nix`, from `sources.nix.in` with the source's name.
{
  lib,
  pkgs,
  # The sources that get the tool configuration.
  sources,
  # The sources that get `nix/sources.nix`.
  wired,
}:
let
  toolFiles = [
    "ruff.toml"
    "ruff-anyio.toml"
  ];

  sourcesNix =
    source:
    pkgs.writeText "sources.nix" (
      builtins.replaceStrings [ "@name@" ] [ source ] (builtins.readFile ./shared/sources.nix.in)
    );

  # Each copy as the path it has in the tree and the file it must equal.
  copies =
    lib.concatMap (
      source:
      map (file: {
        path = "${source}/nix/shared/${file}";
        original = ./shared + "/${file}";
      }) toolFiles
    ) sources
    ++ map (source: {
      path = "${source}/nix/sources.nix";
      original = sourcesNix source;
    }) wired;
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
      { path, original }:
      ''
        mkdir -p ${dirOf path}
        cp --no-preserve=mode ${original} ${path}
      ''
    ) copies}
  '';

  # Is every copy the same bytes as its original?
  check = pkgs.runCommand "shared-config-in-step" { } ''
    ${lib.concatMapStrings (
      { path, original }:
      let
        copy = ../. + "/${path}";
      in
      ''
        if ! ${if builtins.pathExists copy then "cmp --quiet ${original} ${copy}" else "false"}; then
          echo "${path} differs from what nix/shared/ makes. Run: nix run --file . shared-config-update" >&2
          exit 1
        fi
      ''
    ) copies}
    touch $out
  '';
}

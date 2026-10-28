# One Nix formatter for the whole tree: nixfmt. nixidae/nixidae#1104.
{
  lib,
  pkgs,
}:
let
  # pynix-lsp's fixtures: carets in column-0 comments mark positions, and two
  # files do not parse on purpose. nanopynix/treefmt.toml says more.
  excluded = [ "nanopynix/pynix-lsp/tests/test_lsp" ];

  files = lib.fileset.difference (lib.fileset.intersection (lib.fileset.gitTracked ../.) (
    lib.fileset.fileFilter (file: file.hasExt "nix") ../.
  )) (lib.fileset.unions (map (dir: ../. + "/${dir}") excluded));
in
{
  # Formats every tracked .nix file.
  update = pkgs.writeShellScriptBin "nixfmt-update" ''
    set -euo pipefail
    # A secondary jj workspace has no .git. nixidae/nixidae#18.
    if ! root=$(${lib.getExe pkgs.jujutsu} root 2>/dev/null) &&
      ! root=$(${lib.getExe pkgs.git} rev-parse --show-toplevel 2>/dev/null); then
      echo "nixfmt-update: $PWD is in no jj workspace and no git checkout" >&2
      exit 1
    fi
    cd "$root"
    ${lib.getExe pkgs.git} ls-files -z -- '*.nix' ${
      lib.concatMapStringsSep " " (dir: lib.escapeShellArg ":!:${dir}/**") excluded
    } | xargs -0 ${lib.getExe pkgs.nixfmt}
  '';

  # Is every tracked .nix file formatted?
  check =
    pkgs.runCommand "nixfmt-in-step"
      {
        src = lib.fileset.toSource {
          root = ../.;
          fileset = files;
        };
        nativeBuildInputs = [ pkgs.nixfmt ];
      }
      ''
        cd $src
        if ! find . -name '*.nix' -print0 | xargs -0 nixfmt --check; then
          echo "Run: nix run --file . nixfmt-update" >&2
          exit 1
        fi
        touch $out
      '';
}

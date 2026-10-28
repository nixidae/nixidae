# One Nix linter for the whole tree: statix, configured by nix/statix.toml.
# nixidae/nixidae#1104.
{
  lib,
  pkgs,
}:
let
  # pynix-lsp's fixtures: two files do not parse on purpose.
  excluded = [ "nanopynix/pynix-lsp/tests/test_lsp" ];

  files = lib.fileset.difference (lib.fileset.intersection (lib.fileset.gitTracked ../.) (
    lib.fileset.fileFilter (file: file.hasExt "nix") ../.
  )) (lib.fileset.unions (map (dir: ../. + "/${dir}") excluded));
in
{
  # Applies statix's fixes, then formats what they changed.
  fix = pkgs.writeShellScriptBin "statix-fix" ''
    set -euo pipefail
    # A secondary jj workspace has no .git. nixidae/nixidae#18.
    if ! root=$(${lib.getExe pkgs.jujutsu} root 2>/dev/null) &&
      ! root=$(${lib.getExe pkgs.git} rev-parse --show-toplevel 2>/dev/null); then
      echo "statix-fix: $PWD is in no jj workspace and no git checkout" >&2
      exit 1
    fi
    cd "$root"
    ${lib.getExe pkgs.git} ls-files -z -- '*.nix' ${
      lib.concatMapStringsSep " " (dir: lib.escapeShellArg ":!:${dir}/**") excluded
    } | xargs -0 -n 1 ${lib.getExe pkgs.statix} fix --config ${./statix.toml}
    ${lib.getExe pkgs.git} ls-files -z -- '*.nix' ${
      lib.concatMapStringsSep " " (dir: lib.escapeShellArg ":!:${dir}/**") excluded
    } | xargs -0 ${lib.getExe pkgs.nixfmt}
  '';

  # Does statix find nothing in any tracked .nix file?
  check =
    pkgs.runCommand "statix-clean"
      {
        src = lib.fileset.toSource {
          root = ../.;
          fileset = files;
        };
        nativeBuildInputs = [ pkgs.statix ];
      }
      ''
        if ! statix check --config ${./statix.toml} $src; then
          echo "Run: nix run --file . statix-fix" >&2
          exit 1
        fi
        touch $out
      '';
}

# Cargo projects, each with a check that runs what it built.
#
#     nix run --file . run -- rust.cli.serde-app
{ pkgs, pydd }:
let
  projects = {
    # A binary over serde with derive and serde_json: a proc-macro, five
    # build scripts and crates built only for the host.
    serde-app = {
      src = ./serde-app;
      check = ''
        test "$(serde-app)" = '{"text":"hello from serde-app"}'
      '';
    };
  };
in
builtins.mapAttrs (
  name: entry:
  let
    root = pydd.rust.cargo { project = entry.src; };
  in
  {
    out = derivation {
      name = "${name}-check";
      inherit (pkgs.stdenv.hostPlatform) system;
      builder = "${pkgs.bash}/bin/bash";
      PATH = "${root}/bin:${pkgs.coreutils}/bin";
      args = [
        "-c"
        ''
          set -eu
          ${entry.check}
          echo ok > $out
        ''
      ];
    };
    environment = root.dynamic;
    inherit root;
    inherit (root) closure;
  }
) projects

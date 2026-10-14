# npm projects, each with a check that runs what it built.
#
#     nix run --file . run -- node.cli.hello-npm
{ pkgs, pydd }:
let
  projects = {
    # An ES module over chalk 4, which is CommonJS: ansi-styles 6 at the top
    # of the tree and ansi-styles 4 nested under chalk.
    hello-npm = {
      src = ./hello-npm;
      check = ''
        test "$(hello-npm)" = 'hello from hello-npm semver=7 styles=6/4'
      '';
    };
    # better-sqlite3's install script tries prebuild-install, which the
    # node refuses, and then node-gyp compiles the SQLite it bundles.
    native-npm = {
      src = ./native-npm;
      check = ''
        test "$(native-npm)" = 'hello from native-npm sqlite=3 answer=42'
      '';
    };
    # esbuild has a package for each platform, as optional dependencies, and
    # an install script that checks the one it gets. The root runs `npm run
    # build`, and the result holds no development dependency.
    esbuild-app = {
      src = ./esbuild-app;
      check = ''
        test "$(esbuild-app)" = 'hello from esbuild-app day=86400000'
        modules=$(dirname "$(readlink -f "$(command -v esbuild-app)")")/../node_modules
        test -d "$modules/ms"
        test ! -e "$modules/esbuild"
      '';
    };
    # An npm workspace: the program is a member's, and its dependency is the
    # other member, through the link that the lock names.
    workspace = {
      src = ./workspace;
      arguments.program = "workspace-app";
      check = ''
        test "$(workspace-app)" = 'hello from workspace-app hour=3600000'
      '';
    };
  };
in
builtins.mapAttrs (
  name: entry:
  let
    root = pydd.node.npm ({ project = entry.src; } // entry.arguments or { });
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

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
    # openssl and zstd from nixpkgs, through pkg-config, with no declaration:
    # `defaultCrateOverrides` gives both (Lillecarl/pydd#17). Its entry for
    # zstd-sys sets ZSTD_SYS_USE_PKG_CONFIG, so the binary links the system
    # libzstd rather than the copy that zstd-sys bundles.
    native-app = {
      src = ./native-app;
      check = ''
        test "$(native-app)" = 'hello from native-app sha256=ecd4 openssl=3'
        needed=$(readelf -d "$(command -v native-app)")
        echo "$needed"
        grep -q 'libssl.so' <<< "$needed"
        grep -q 'libzstd.so' <<< "$needed"
      '';
    };
    # The same, with the hints off and each input declared: openssl-sys in
    # `[package.metadata.pydd.build-tools]`, zstd-sys in the arguments.
    native-declared = {
      src = ./native-declared;
      arguments = {
        nixpkgsHints = false;
        buildTools.zstd-sys = [
          pkgs.pkg-config
          pkgs.zstd.dev
          pkgs.zstd
        ];
        overrides.zstd-sys.env.ZSTD_SYS_USE_PKG_CONFIG = "1";
      };
      check = ''
        test "$(native-declared)" = 'hello from native-declared sha256=3d5a openssl=3'
        needed=$(readelf -d "$(command -v native-declared)")
        grep -q 'libssl.so' <<< "$needed"
        grep -q 'libzstd.so' <<< "$needed"
      '';
    };
    # A virtual workspace: a member with a build script that writes OUT_DIR,
    # a path dependency on it, a git dependency at a tag, and a feature that
    # crosses the members.
    workspace = {
      src = ./workspace;
      arguments = {
        name = "workspace";
        features = [ "ws-app/shout" ];
        outputHashes."itoa-1.0.15" = "sha256-7be7w9eSbX6d6nb6zaTNvp8gt8pxYdAQLGaaKfaUUns=";
      };
      check = ''
        test "$(ws-app)" = 'HELLO FROM THE WORKSPACE 42'
      '';
    };
    # A path dependency outside the project: `root` holds both.
    monorepo = {
      src = ./monorepo/tool;
      arguments.root = ./monorepo;
      check = ''
        test "$(mono-tool)" = 'hello from outside the project'
      '';
    };
  };
in
builtins.mapAttrs (
  name: entry:
  let
    root = pydd.rust.cargo ({ project = entry.src; } // entry.arguments or { });
  in
  {
    out = derivation {
      name = "${name}-check";
      inherit (pkgs.stdenv.hostPlatform) system;
      builder = "${pkgs.bash}/bin/bash";
      PATH = "${root}/bin:${pkgs.coreutils}/bin:${pkgs.gnugrep}/bin:${pkgs.binutils-unwrapped}/bin";
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

# SPDX-License-Identifier: MIT

# Cache uses lruLix which is Lix patched to keep storePaths registrationTime up2date when they're referenced
# through the Daemon protocol
{
  pkgs,
  dinix,
}:
let
  lib = pkgs.lib;
  dinixEval = import dinix {
    inherit pkgs;
    modules = [
      ../modules/gc.nix
      ../modules/ssh.nix
      ../modules/users.nix
      ../modules/nix-daemon.nix
      ../modules/setup.nix
      ../modules/logger.nix
      {
        config = {
          gc = {
            retainSeconds = 86400;
            intervalSeconds = 3600;
          };
          # Umbrella service for cache
          services.cache = {
            type = "internal";
            depends-on = [
              "logger"
              "gc"
              "openssh"
              "ssh-reloader"
            ];
          };
        };
      }
    ];
  };

  cacheEnv = pkgs.buildEnv {
    name = "cacheEnv";
    paths = with pkgs; [
      dinixEval.config.containerWrapper
      bash
      coreutils
      lruLix
      openssh
      pynixd-nixkube
      pynixd-nixkube.fakeNss
      tini
      # dev
      fishMinimal
    ];
    # So we can peek into eval
    passthru.dinixEval = dinixEval;
  };

  initCopy = pkgs.writeShellApplication {
    name = "initCopy";
    runtimeInputs = [
      pkgs.lruLix
      pkgs.rsync
    ];
    text = # bash
      ''
        set -euo pipefail
        set -x
        # AI: This isn't a duplicate with the setup since they occur in different containers
        rsync --archive ${pkgs.dockerTools.caCertificates}/ /
        # Install environment into persistent volume
        nix build \
          --extra-substituters "local?trusted=true&read-only=true" \
          --store /nix-volume \
          --out-link /nix-volume/nix/var/result \
          /nix/var/result
      '';
  };
in
pkgs.buildEnv {
  name = "cache-init-env";
  paths = [
    cacheEnv
    initCopy
  ];
}

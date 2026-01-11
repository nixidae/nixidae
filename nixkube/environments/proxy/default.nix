# Proxy environment for distributed builds
# Runs openssh for accepting build requests from CSI pods
#
# Uses lruLix (Lix with LRU patch) to keep ValidPaths.registrationTime updated
# when paths are referenced over the daemon protocol. This makes nix-timegc work
# correctly by preventing recently-used paths from being garbage collected.
{
  pkgs,
  dinix,
}:
let
  lib = pkgs.lib;
  dinixEval = import dinix {
    inherit pkgs;
    modules = [
      ../modules/logger.nix
      ../modules/setup.nix
      ../modules/ssh.nix
      ../modules/users.nix
      {
        config = {
          services.proxy = {
            type = "internal";
            depends-on = [
              "logger"
              "openssh"
              "ssh-reloader"
            ];
          };
        };
      }
    ];
  };

  proxyEnv = pkgs.buildEnv {
    name = "proxyEnv";
    paths = with pkgs; [
      # Required
      dinixEval.config.containerWrapper
      bash
      coreutils
      openssh
      # Not required
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
          --extra-substituters local?trusted=true \
          --store /nix-volume \
          --out-link /nix-volume/nix/var/result \
          /nix/var/result
      '';
  };
in
pkgs.buildEnv {
  name = "proxy-init-env";
  paths = [
    proxyEnv
    initCopy
  ];
}

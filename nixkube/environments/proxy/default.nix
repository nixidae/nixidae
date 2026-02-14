# SPDX-License-Identifier: MIT

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
      ../modules/ssh.nix
      ../modules/users.nix
      {
        config = {
          users.users.root.homeDir = lib.mkForce "/root";
          services.setup = {
            type = "scripted";
            log-type = "file";
            logfile = "/var/log/setup.log";
            options = [ "starts-rwfs" ];
            command = pkgs.writeShellApplication {
              name = "setup";
              runtimeInputs = [
                pkgs.rsync
                pkgs.coreutils
              ];
              text = # bash
                ''
                  set -euo pipefail
                  set -x
                  mkdir --parents {/tmp,/var/tmp}
                  chmod -R 1777 {/tmp,/var/tmp}
                  mkdir --parents /var/log
                  chmod -R 0755 /var/log
                  # Copy in "well-known paths" into container root
                  rsync --archive ${pkgs.dockerTools.binSh}/ /
                  rsync --archive ${pkgs.dockerTools.caCertificates}/ /
                  rsync --archive ${pkgs.dockerTools.usrBinEnv}/ /
                '';
            };
          };
          services.proxy = {
            type = "internal";
            depends-on = [
              "setup"
              "logger"
              "openssh"
              "ssh-reloader"
            ];
          };
        };
      }
    ];
  };
in
pkgs.buildEnv {
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
}

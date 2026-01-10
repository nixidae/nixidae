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
      ../modules/users.nix
      ../modules/nix-daemon.nix
      {
        config = {
          gc = {
            retainSeconds = 3600;
            intervalSeconds = 3600;
          };
          env-file.variables = {
            PYTHONUNBUFFERED = "1"; # If something ends up print logging
            NIXPKGS_ALLOW_UNFREE = "1"; # Allow building anything
          };
          services.setup = {
            type = "scripted";
            log-type = "file";
            logfile = "/var/log/setup.log";
            command = pkgs.writeShellApplication {
              name = "setup";
              runtimeInputs = [
                pkgs.rsync
                pkgs.coreutils
                pkgs.lruLix
              ];
              text = # bash
                ''
                  set -euo pipefail
                  set -x
                  mkdir --parents {/tmp,/var/tmp}
                  chmod -R 1777 {/tmp,/var/tmp}
                  mkdir --parents {/var/log,/nix/var/nix-csi}
                  chmod -R 0755 {/var/log,/nix/var/nix-csi}
                  rsync --archive ${pkgs.dockerTools.binSh}/ /
                  rsync --archive ${pkgs.dockerTools.caCertificates}/ /
                  rsync --archive ${pkgs.dockerTools.usrBinEnv}/ /
                  # Fix gcroots for /nix/var/result. The one created by initCopy
                  # points to invalid symlinks in the chain
                  # (auto -> /nix-volume/var/result) rather than
                  # (auto -> /nix/var/result). The link back to store works
                  # though so this just fixes gcroots.
                  nix build --store local --out-link /nix/var/result /nix/var/result
                '';
            };
          };
          # Umbrella service for CSI
          services.csi = {
            type = "scripted";
            options = [ "starts-rwfs" ];
            command = pkgs.writeShellApplication {
              name = "csi";
              text = # bash
                ''
                  mkdir --parents /run
                  mkdir --parents /var/log
                '';
            };
            depends-on = [
              "csi-daemon"
              "logger"
            ];
          };
          services.csi-daemon = {
            command = "${lib.getExe pkgs.nix-csi} --loglevel DEBUG";
            log-type = "file";
            logfile = "/var/log/csi-daemon.log";
            depends-on = [
              "setup"
              "gc"
              "nix-daemon"
            ];
          };
          services.logger = {
            command = "${lib.getExe' pkgs.coreutils "tail"} --retry --follow=name /var/log/csi-daemon.log /var/log/dinit.log /var/log/ssh.log /var/log/setup.log /var/log/gc.log";
            options = [ "shares-console" ];
          };
        };
      }
    ];
  };
  pathEnv = pkgs.buildEnv {
    name = "nodeEnv";
    paths = with pkgs; [
      dinixEval.config.containerWrapper
      bash # Used for build and upload scripts
      coreutils
      fishMinimal
      lruLix
      openssh
      util-linuxMinimal
      gnugrep
      getent
      doggo
      iputils
      curl
    ];
    # So we can peek into eval
    passthru.dinixEval = dinixEval;
  };
in
pathEnv

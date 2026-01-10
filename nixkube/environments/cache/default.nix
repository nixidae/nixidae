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
      {
        config = {
          gc = {
            retainSeconds = 86400;
            intervalSeconds = 3600;
          };
          # services.nix-serve-ng = {
          #   command = "${lib.getExe pkgs.lixPackageSets.lix_2_93.nix-serve-ng}";
          #   log-type = "file";
          #   logfile = "/var/log/nix-serve-ng.log";
          # };
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
                  # Copy in "well-known paths" into container root
                  rsync --archive ${pkgs.dockerTools.binSh}/ /
                  rsync --archive ${pkgs.dockerTools.caCertificates}/ /
                  rsync --archive ${pkgs.dockerTools.usrBinEnv}/ /
                  # Fix gcroots for /nix/var/result. The one created by initCopy
                  # points to invalid symlinks in the chain
                  # (auto -> /nix-volume/var/result) rather than
                  # (auto -> /nix/var/result). The link back to store works
                  # though so this just fixes gcroots.
                  # /nix/var/result will always exist, else the initContainer will fail
                  nix build --store local --out-link /nix/var/result /nix/var/result
                '';
            };
          };
          # Umbrella service for cache
          services.cache = {
            type = "scripted";
            options = [ "starts-rwfs" ];
            command = pkgs.writeShellApplication {
              name = "cache";
              text = # bash
                ''
                  mkdir --parents /run
                  mkdir --parents /var/log
                '';
            };
            depends-on = [
              "logger"
              "gc"
              "openssh"
              "ssh-reloader"
            ];
          };
          services.logger = {
            command = "${lib.getExe' pkgs.coreutils "tail"} --retry --follow=name /var/log/dinit.log /var/log/ssh.log /var/log/setup.log /var/log/setup.log /var/log/nix-serve-ng.log /var/lib/gc.log";
            options = [ "shares-console" ];
          };
        };
      }
    ];
  };

  cacheEnv = pkgs.buildEnv {
    name = "cacheEnv";
    paths = with pkgs; [
      dinixEval.config.containerWrapper
      bash # Used for build and upload scripts
      procps # pkill
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
  name = "initEnv";
  paths = [
    cacheEnv
    initCopy
  ];
}

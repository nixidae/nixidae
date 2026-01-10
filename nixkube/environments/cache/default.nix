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
      {
        config = {
          users = {
            enable = true;
            users.root = {
              shell = pkgs.runtimeShell;
              homeDir = "/nix/var/nix-csi/root";
            };
            users.nix = {
              uid = 1000;
              gid = 1000;
              comment = "Nix worker user";
            };
            groups.nix.gid = 1000;
            groups.nixbld.gid = 30000;
            users.sshd = {
              uid = 993;
              gid = 992;
              comment = "SSH privilege separation user";
            };
            groups.sshd.gid = 992;
          };
          services.openssh = {
            type = "process";
            command = "${lib.getExe' pkgs.openssh "sshd"} -D -f /etc/ssh/sshd_config -e";
            depends-on = [ "setup" ];
            log-type = "file";
            logfile = "/var/log/ssh.log";
          };
          services.ssh-reloader = {
            type = "process";
            command = pkgs.writeShellApplication {
              name = "ssh-reloader";
              runtimeInputs = [
                pkgs.procps
                pkgs.inotify-tools
              ];
              text = # bash
                ''
                  sleep 10
                  inotifywait -m -e create,moved_to /etc/ssh-key/ | \
                  while read -r _ _ filename; do
                    if [[ "$filename" == "..data" ]]; then
                      pkill -HUP -o sshd
                    fi
                  done
                '';
            };
            depends-on = [ "openssh" ];
          };
          services.nix-daemon = {
            command = "${lib.getExe pkgs.lruLix} daemon --store local";
            depends-on = [ "setup" ];
            log-type = "file";
            logfile = "/var/log/nix-daemon.log";
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
            command =
              pkgs.writeScriptBin "setup" # bash
                ''
                  #! ${pkgs.runtimeShell}
                  set -euo pipefail
                  set -x
                  export PATH=${
                    lib.makeBinPath (
                      with pkgs;
                      [
                        rsync
                        coreutils
                        lruLix
                      ]
                    )
                  }
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
          # Umbrella service for cache
          services.cache = {
            type = "scripted";
            options = [ "starts-rwfs" ];
            command =
              pkgs.writeScriptBin "cache" # bash
                ''
                  #! ${pkgs.runtimeShell}
                  mkdir --parents /run
                  mkdir --parents /var/log
                '';
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
          services.gc = {
            command =
              pkgs.writeScriptBin "gc" # bash
                ''
                  #! ${pkgs.runtimeShell}
                  # Collect old paths occasionally
                  # TODO: Copy to cache here too
                  while :; do
                    ${lib.getExe pkgs.nix-timegc} 86400
                    SLEEP=$(shuf -i 1800-3600 -n 1)
                    echo Sleeping for $SLEEP seconds
                    sleep $SLEEP
                  done
                '';
            log-type = "file";
            logfile = "/var/log/gc.log";
            depends-on = [
              "setup"
              "nix-daemon"
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

  initCopy =
    pkgs.writeScriptBin "initCopy" # bash
      ''
        #! ${pkgs.runtimeShell}
        export PATH=${
          lib.makeBinPath [
            pkgs.lruLix
            pkgs.rsync
          ]
        }
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
in
pkgs.buildEnv {
  name = "initEnv";
  paths = [
    cacheEnv
    initCopy
  ];
}

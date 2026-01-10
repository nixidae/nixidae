# Builder environment for distributed builds
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
          env-file.variables = {
            PYTHONUNBUFFERED = "1";
            NIXPKGS_ALLOW_UNFREE = "1";
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
          # Umbrella service for builder
          services.builder = {
            type = "scripted";
            options = [ "starts-rwfs" ];
            command =
              pkgs.writeScriptBin "builder" # bash
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
            command = "${lib.getExe' pkgs.coreutils "tail"} --retry --follow=name /var/log/nix-daemon.log /var/log/dinit.log /var/log/ssh.log /var/log/setup.log /var/log/gc.log";
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
                    ${lib.getExe pkgs.nix-timegc} 3600
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

  builderEnv = pkgs.buildEnv {
    name = "builderEnv";
    paths = with pkgs; [
      # Required
      dinixEval.config.containerWrapper
      bash
      coreutils
      gitMinimal
      lruLix
      openssh
      procps # pgrep
      # Commonly used
      attic-client
      cachix
      # Not required
      fishMinimal
      gnugrep
      getent
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
          --store /nix-volume \
          --out-link /nix-volume/nix/var/result \
          ${builderEnv}
      '';
in
pkgs.buildEnv {
  name = "initEnv";
  paths = [
    builderEnv
    initCopy
  ];
}

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
            PYTHONUNBUFFERED = "1"; # If something ends up print logging
            NIXPKGS_ALLOW_UNFREE = "1"; # Allow building anything
          };
          services.nix-daemon = {
            command = "${lib.getExe pkgs.lixPackageSets.lix_2_93.lix} daemon --store local";
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
                        lixPackageSets.lix_2_93.lix
                      ]
                    )
                  }
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
          # Umbrella service for CSI
          services.csi = {
            type = "scripted";
            options = [ "starts-rwfs" ];
            command =
              pkgs.writeScriptBin "csi" # bash
                ''
                  #! ${pkgs.runtimeShell}
                  mkdir --parents /run
                  mkdir --parents /var/log
                '';
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
  pathEnv = pkgs.buildEnv {
    name = "nodeEnv";
    paths = with pkgs; [
      dinixEval.config.containerWrapper
      bash # Used for build and upload scripts
      coreutils
      fishMinimal
      lixPackageSets.lix_2_93.lix
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

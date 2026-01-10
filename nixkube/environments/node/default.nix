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
      ../modules/logger.nix
      ../modules/nix-daemon.nix
      ../modules/setup.nix
      ../modules/users.nix
      {
        config = {
          gc = {
            retainSeconds = 3600;
            intervalSeconds = 3600;
          };
          logger.files = [
            "csi-daemon.log"
          ];
          env-file.variables = {
            PYTHONUNBUFFERED = "1"; # If something ends up print logging
            NIXPKGS_ALLOW_UNFREE = "1"; # Allow building anything
          };
          # Umbrella service for CSI
          services.csi = {
            type = "internal";
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

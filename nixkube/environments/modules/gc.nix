# SPDX-License-Identifier: MIT

{
  pkgs,
  lib,
  config,
  ...
}:
{
  options.gc = {
    retainSeconds = lib.mkOption {
      type = lib.types.ints.positive;
    };
    intervalSeconds = lib.mkOption {
      type = lib.types.ints.positive;
    };
  };
  config = {
    logger.files = [
      "gc.log"
    ];
    services.gc = {
      command = pkgs.writeShellApplication {
        name = "gc";
        runtimeInputs = [
          pkgs.lruLix
          pkgs.jq
        ];
        text =
          let
            rs = toString config.gc.retainSeconds;
            lis = toString (config.gc.intervalSeconds / 2);
            uis = toString config.gc.intervalSeconds;
          in
          # bash
          ''
            while :; do
              # Copy everything to cache
              PYNIXD_ENABLED="''${PYNIXD_ENABLED:-"false"}"
              GC_KEEP_SECONDS="''${GC_KEEP_SECONDS:-"3600"}"
              IS_CACHE="''${IS_CACHE:-"false"}"
              if test "$PYNIXD_ENABLED" = "true"; then
                nix copy --all --store local --to ssh-ng://nix@pynixd || true
              fi
              # Garbage collect anything older than an hour
              echo "Collecting garbage"
              nix path-info --store local --all --json | \
                jq -r --argjson age "$GC_KEEP_SECONDS" 'map(select(.registrationTime < (now - $age)) | .path) | .[]' | \
                nix store delete --store local --stdin --skip-live
              if test "$IS_CACHE" = "true"; then
                echo "Optimising Nix store (hardlinking)"
                nix store optimise
                echo "Signing all storepaths (this needs to be hooked somehow)"
                nix path-info --all | nix store sign --stdin --key-file /etc/nix-key/nix_ed25519 
              fi
              # chill
              SLEEP=$(shuf -i ${lis}-${uis} -n 1)
              echo Sleeping for "$SLEEP" seconds
              sleep "$SLEEP"
            done
          '';
      };
      log-type = "file";
      logfile = "/var/log/gc.log";
      depends-on = [
        "setup"
        "nix-daemon"
      ];
    };
  };
}

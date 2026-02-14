# SPDX-License-Identifier: MIT

{
  pkgs,
  ...
}:
{
  config = {
    logger.files = [
      "setup.log"
    ];
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
  };
}

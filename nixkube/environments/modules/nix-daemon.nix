# SPDX-License-Identifier: MIT

{
  pkgs,
  lib,
  ...
}:
{
  config = {
    services.nix-daemon = {
      command = "${lib.getExe pkgs.lruLix} daemon --store local";
      depends-on = [ "setup" ];
      log-type = "file";
      logfile = "/var/log/nix-daemon.log";
    };
  };
}

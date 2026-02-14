# SPDX-License-Identifier: MIT

{ pkgs, lib, ... }:
{
  config = {
    logger.files = [
      "ssh.log"
    ];
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
  };
}

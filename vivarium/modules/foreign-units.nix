# The systemd units that run the vivarium agent and the journal stream
# on a guest that is not NixOS, from the store, through the guest's own
# systemd and journalctl. image-guest.nix and vyos-guest.nix install them.
{ lib, pkgs, runner }:
let
  # What the agent's commands find on PATH: the distribution's own.
  guestPath = "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin";
in
pkgs.linkFarm "vivarium-image-units" {
  "vivarium-agent.service" = pkgs.writeText "vivarium-agent.service" ''
    [Unit]
    Description=Host control channel on /dev/ttyS0
    After=dev-ttyS0.device vivarium-journal.service

    [Service]
    Environment=VIVARIUM_AGENT_DEVICE=/dev/ttyS0
    Environment=VIVARIUM_GUEST_PATH=${guestPath}
    ExecStart=${lib.getExe' runner "vivarium-agent"}
    StandardOutput=journal+console
    StandardError=journal+console
  '';
  # The same fields as guest.nix's, through the distribution's own
  # journalctl.
  "vivarium-journal.service" = pkgs.writeText "vivarium-journal.service" ''
    [Unit]
    Description=Stream the journal to the host
    RequiresMountsFor=/artifacts

    [Service]
    ExecStart=journalctl --follow --boot --output=json --output-fields=MESSAGE,PRIORITY,_SYSTEMD_UNIT,SYSLOG_IDENTIFIER,_PID,_TRANSPORT
    StandardOutput=truncate:/artifacts/journal.jsonl
  '';
}

/*
  Can a phase switch a running guest to another configuration and
  back, as `nixos-rebuild` would, without losing the agent?
*/
{
  pkgs,
  lib,
  mkTest,
  ...
}:
mkTest {
  name = "switch";
  nodes.one =
    { pkgs, ... }:
    {
      environment.etc."vivarium-config".text = "one\n";
      vivarium.configurations.two = {
        environment.etc."vivarium-config".text = lib.mkForce "two\n";
        systemd.services.only-in-two = {
          wantedBy = [ "multi-user.target" ];
          serviceConfig = {
            Type = "oneshot";
            RemainAfterExit = true;
            ExecStart = "${pkgs.coreutils}/bin/true";
          };
        };
        # A changed agent unit: without `restartIfChanged = false` the
        # switch restarts the agent under the command that runs it.
        systemd.services.vivarium-agent.environment.VIVARIUM_SWITCHED = "1";
      };
    };
  phases.switch = {
    script = ../phases/switch.py;
    after = [ "boot" ];
  };
}

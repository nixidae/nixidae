# E10: a NixOS guest as the router. `plain` is a default vivarium guest;
# `slim` turns off what a router does not use.
#
#   nix run --file lab/router/nixos e10.uml.driver -- --out DIR
{
  sources ? import ../../../nix/sources.nix,
  pkgs ? import sources.nixpkgs { },
}:
let
  vivarium = import ../../.. { inherit sources pkgs; };
in
{
  e10 = vivarium.lib.mkTest {
    name = "e10";
    phases.probe = {
      script = ./probe.py;
      after = [ "boot" ];
    };
    nodes.plain = {
      vivarium.memory = "256M";
    };
    nodes.slim =
      { lib, ... }:
      {
        vivarium.memory = "256M";
        vivarium.nixDatabase.enable = false;
        nix.enable = false;
        services.udev.enable = lib.mkForce false;
        systemd.services.systemd-journal-flush.enable = false;
        systemd.oomd.enable = false;
        services.timesyncd.enable = false;
        security.audit.enable = false;
        environment.defaultPackages = [ ];
      };
    nodes.slimmer =
      { lib, ... }:
      {
        vivarium.memory = "256M";
        vivarium.nixDatabase.enable = false;
        nix.enable = false;
        systemd.oomd.enable = false;
        services.timesyncd.enable = false;
        security.audit.enable = false;
        environment.defaultPackages = [ ];
        services.openssh.enable = lib.mkForce false;
        services.resolved.enable = false;
        services.nscd.enable = false;
        system.nssModules = lib.mkForce [ ];
        systemd.suppressedSystemUnits = [
          "systemd-udevd.service"
          "systemd-udevd-control.socket"
          "systemd-udevd-kernel.socket"
          "systemd-udev-trigger.service"
          "systemd-udev-settle.service"
          "systemd-journal-catalog-update.service"
          "systemd-update-done.service"
          "lastlog2-import.service"
        ];
        security.enableWrappers = false;
        # No udev, so no device unit for the agent's tty.
        systemd.services.vivarium-agent.after = lib.mkForce [ ];
        systemd.services.vivarium-agent.bindsTo = lib.mkForce [ ];
      };
  };
}

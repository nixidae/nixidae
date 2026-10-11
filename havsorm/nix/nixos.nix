/**
  Salt with havsorm on NixOS: nixpkgs' `services.salt.master` and
  `services.salt.minion`, run from a Python that holds Salt and havsorm.

  Import it beside the nixpkgs modules and set `services.havsorm.package`
  to havsorm's `salt` environment.
*/
{
  config,
  lib,
  ...
}:
let
  cfg = config.services.havsorm;
  master = config.services.salt.master;
  minion = config.services.salt.minion;
in
{
  options.services.havsorm.package = lib.mkOption {
    type = lib.types.package;
    description = "A Python environment with Salt and havsorm: havsorm's `salt` attribute.";
  };

  config = lib.mkMerge [
    (lib.mkIf (master.enable || minion.enable) {
      # Above nixpkgs' plain Salt, which both modules also install.
      environment.systemPackages = [ (lib.hiPrio cfg.package) ];
    })
    (lib.mkIf master.enable {
      systemd.services.salt-master.serviceConfig.ExecStart =
        lib.mkForce "${cfg.package}/bin/salt-master";
    })
    (lib.mkIf minion.enable {
      systemd.services.salt-minion = {
        serviceConfig.ExecStart = lib.mkForce "${cfg.package}/bin/salt-minion";
        # `havsorm.system` runs switch-to-configuration from a job of
        # this minion. A restart in that switch kills the job, and the
        # master never gets its return.
        restartIfChanged = false;
      };
    })
  ];
}

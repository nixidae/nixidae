/*
  Does a guest on `vivarium.efi` boot through systemd-boot, and does
  systemd-boot fall back from a generation that never completes a
  boot? The same claims as nixpkgs' `systemd-boot.bootCounting`, on
  vivarium's firmware boot.

  `good` and `bad` differ from the booted system by one file each.
  `bad` also has a unit that `boot-complete.target` requires and that
  fails, so systemd-bless-boot never marks its entry good.
*/
{ mkTest, ... }:
mkTest (
  { systems, ... }:
  let
    next =
      name: extra:
      "${(systems.one.extendModules {
        modules = [
          {
            environment.etc."vivarium-generation".text = "${name}\n";
          }
          extra
        ];
      }).config.system.build.toplevel
      }";
  in
  {
    name = "boot-counting";
    backend = "qemu";
    phases.test = {
      script = ../boot-counting.py;
      after = [ "boot" ];
    };
    nodes.one =
      { settings, ... }:
      {
        vivarium.lateClosure = [
          settings.good
          settings.bad
        ];
        vivarium.efi = true;
        boot.loader.systemd-boot.bootCounting = {
          enable = true;
          tries = 2;
        };
      };
    settings = {
      good = next "good" { };
      bad = next "bad" {
        systemd.services.vivarium-bad-boot = {
          requiredBy = [ "boot-complete.target" ];
          before = [ "boot-complete.target" ];
          serviceConfig.Type = "oneshot";
          script = "exit 1";
        };
      };
    };
  }
)

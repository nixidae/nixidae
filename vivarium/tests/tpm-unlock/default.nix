/*
  Does a QEMU guest unlock LUKS from its TPM in the initrd, through
  systemd-boot on Secure Boot firmware? And does it stop at the
  passphrase prompt once the TPM can no longer unlock it?

  The first boot cannot name the LUKS device: the blank disk is empty
  then. `unlock` is the booted system plus the device in its initrd.
*/
{ mkTest, ... }:
mkTest (
  { systems, ... }:
  {
    name = "tpm-unlock";
    backend = "qemu";
    phases.test = {
      script = ../tpm-unlock.py;
      after = [ "boot" ];
    };
    nodes.one =
      { pkgs, settings, ... }:
      {
        vivarium.lateClosure = [ settings.unlock ];
        vivarium.efi = true;
        vivarium.secureBoot = true;
        vivarium.tpm.enable = true;
        vivarium.disks = [
          {
            name = "secret";
            size = 64;
          }
        ];
        environment.systemPackages = [ pkgs.cryptsetup ];
      };
    settings.unlock = "${(systems.one.extendModules {
      modules = [
        {
          boot.initrd.luks.devices.secret = {
            device = "/dev/disk/by-id/virtio-secret";
            crypttabExtraOpts = [ "tpm2-device=auto" ];
          };
        }
      ];
    }).config.system.build.toplevel
    }";
  }
)

# The router lab (docs/design/router-lab.md). Not part of any check.
#
#   nix build --file lab/router initrd
{
  sources ? import ../../nix/sources.nix,
  pkgs ? import sources.nixpkgs { },
}:
let
  static = pkgs.pkgsStatic;
  vivarium = import ../.. { inherit sources pkgs; };
in
{
  # The router options in pkgs/uml-kernel, plus ./vsock-linear-tx.patch.
  # Build it with the ccache flags in docs/howto.md.
  kernel = vivarium.internal.umlKernelCcache.overrideAttrs (old: {
    patches = old.patches ++ [ ./vsock-linear-tx.patch ];
  });

  # rust-vmm's main: 0.3.0 reads a TX packet's data from one descriptor
  # (virtio-vsock 0.10), and Linux 7.2 sends a large write as several,
  # so a 64 KiB write stalls with `DescriptorLengthTooSmall`. Main reads
  # any chain (virtio-vsock 0.12).
  vhost-device-vsock = pkgs.vhost-device-vsock.overrideAttrs (old: rec {
    version = "0.3.0-unstable-2026-10-29";
    src = pkgs.fetchFromGitHub {
      owner = "rust-vmm";
      repo = "vhost-device";
      rev = "0484a75df5c8e77a708af10b24dd34f596b25084";
      hash = "sha256-mxeJzo+yGZrLl1tNrWaOxFoX3+8W3AZH6s8L3qRpIeg=";
    };
    cargoDeps = pkgs.rustPlatform.fetchCargoVendor {
      inherit src;
      hash = "sha256-jDzWzVV8IU/Y9xinygy2AsVH9OwlmMUlck0z+JcQguE=";
    };
    doCheck = false;
  });

  # A whole router root: busybox, socat and /init. UML reads it with
  # `initrd=`, so it needs no disk and no store.
  initrd =
    pkgs.runCommand "router-initrd.cpio"
      {
        nativeBuildInputs = [ pkgs.cpio ];
      }
      ''
        mkdir -p root/bin root/dev root/proc root/sys root/tmp
        cp ${static.busybox}/bin/busybox root/bin/
        cp ${static.socat}/bin/socat root/bin/
        for tool in sh mount cat echo dd sed sleep poweroff ip ls date; do
          ln -s busybox root/bin/$tool
        done
        install -m 0755 ${./init.sh} root/init
        (cd root && find . | cpio -o -H newc --owner 0:0) > $out
      '';
}

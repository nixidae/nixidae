# The router lab (docs/design/router-lab.md). Not part of any check.
#
#   nix build --file lab/router initrd
{
  sources ? import ../../nix/sources.nix,
  pkgs ? import sources.nixpkgs { },
}:
let
  inherit (pkgs) lib;
  static = pkgs.pkgsStatic;
  vivarium = import ../.. { inherit sources pkgs; };

  egressPackage =
    rustPlatform:
    rustPlatform.buildRustPackage {
      pname = "vivarium-egress";
      version = "0.0.0";
      src = lib.fileset.toSource {
        root = ./egress;
        fileset = lib.fileset.unions [
          ./egress/Cargo.toml
          ./egress/Cargo.lock
          ./egress/src
        ];
      };
      cargoLock.lockFile = ./egress/Cargo.lock;
    };

  # The router's relay and the client's iperf3, static: the initrd has
  # no store.
  relay = egressPackage static.rustPlatform;

  libkrun = pkgs.callPackage ../../pkgs/libkrun/package.nix { };
  krunKernel = pkgs.linuxPackages.kernel;
  moduleClosure = pkgs.makeModulesClosure {
    kernel = krunKernel.modules;
    firmware = pkgs.emptyDirectory;
    rootModules = [
      "virtio_mmio"
      "virtio_net"
      "vmw_vsock_virtio_transport"
      "nf_tables"
      "nft_redir"
      "nft_chain_nat"
      "nft_nat"
      "nft_tproxy"
      "nft_socket"
      "nft_queue"
      "nf_conntrack"
    ];
  };
in
rec {
  inherit relay;
  egress = egressPackage pkgs.rustPlatform;

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

  # One root for the router and the client: busybox, socat, nft, iperf3,
  # the relay and /init. UML reads it with `initrd=`, so it needs no disk
  # and no store. `lab=` on the kernel line picks the role.
  initrd =
    pkgs.runCommand "router-initrd.cpio"
      {
        nativeBuildInputs = [ pkgs.cpio ];
      }
      ''
        mkdir -p root/bin root/dev root/proc root/sys root/tmp root/etc
        cp ${static.busybox}/bin/busybox root/bin/
        cp ${static.socat}/bin/socat root/bin/
        cp ${static.nftables}/bin/nft root/bin/
        cp ${static.iperf3}/bin/iperf3 root/bin/
        cp ${relay}/bin/relay root/bin/
        for tool in sh mount cat echo dd sed sleep poweroff reboot modprobe ip ls date ping; do
          ln -s busybox root/bin/$tool
        done
        install -m 0755 ${./init.sh} root/init
        install -m 0644 ${./router.nft} root/etc/router.nft
        (cd root && find . | cpio -o -H newc --owner 0:0) > $out
      '';

  # E2: the stock NixOS kernel under libkrun. libkrun loads an ELF, so
  # the dev output's vmlinux, stripped.
  krunVmlinux = pkgs.runCommand "vmlinux-${krunKernel.version}" {
    nativeBuildInputs = [ pkgs.binutils ];
  } "objcopy --strip-all ${krunKernel.dev}/vmlinux $out";

  # The modules the router needs, uncompressed: busybox modprobe reads
  # no xz. The kernel unpacks a second cpio over the first.
  krunModules =
    pkgs.runCommand "router-modules.cpio"
      {
        nativeBuildInputs = [
          pkgs.cpio
          pkgs.xz
          pkgs.kmod
        ];
      }
      ''
        mkdir -p root/lib
        cp -r ${moduleClosure}/lib/modules root/lib/
        chmod -R u+w root
        find root -name '*.ko.xz' -exec xz -d {} +
        depmod -b root -a ${krunKernel.modDirVersion}
        (cd root && find . | cpio -o -H newc --owner 0:0) > $out
      '';

  krunInitrd = pkgs.runCommand "router-krun-initrd.cpio" { } "cat ${initrd} ${krunModules} > $out";

  krun-router = pkgs.stdenv.mkDerivation {
    name = "krun-router";
    src = ./krun-router.c;
    dontUnpack = true;
    buildInputs = [ libkrun ];
    buildPhase = "$CC -std=gnu11 -O2 -Wall -Wextra -Werror -o krun-router $src -lkrun";
    installPhase = "install -D krun-router $out/bin/krun-router";
  };
}

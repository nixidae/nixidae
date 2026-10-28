/**
  Other distributions' cloud images, for `vivarium.image`, and vendor
  images for `vivarium.appliance`.

  An image floats. openSUSE deletes a numbered build when the next one
  lands, so a pinned URL breaks with no commit (nixidae/nixidae#19).
  Evaluation reads the checksum that openSUSE publishes beside the
  floating name, and the download stays a fixed-output derivation with
  that hash.

  Needs impure evaluation: `builtins.fetchurl` without a hash is refused
  under `--pure-eval`. A run that crosses a new openSUSE build fails the
  hash once, and the next evaluation picks the new one up.
*/
{
  fetchurl,
  fetchzip,
  runCommand,
  unzip,
  xorriso,
  squashfsTools,
  python3,
  e2fsprogs,
  util-linux,
  fakeroot,
  libarchive,
  gzip,
  qemu-utils,
}:
let
  /*
    A VyOS rolling ISO as an appliance that boots with a given config.

    VyOS is a live system even when installed: one partition labelled
    `persistence` holds `boot/<version>/<version>.squashfs`, the overlay
    `boot/<version>/rw` above it, and `persistence.conf` at its root, and
    the kernel finds them by `vyos-union=/boot/<version>`. That layout is
    built here, with the config at `rw/opt/vyatta/etc/config/config.boot`,
    so VyOS loads it at boot. Measured, 2026-10-26: `persistence.conf`
    anywhere but the partition root, a filesystem on the bare disk, or the
    ISO's own `/live` as the medium, each boots on an empty tmpfs overlay
    with the default config and says nothing.

    The config gets the version footer this image writes itself, from the
    image's own component versions. Without it VyOS runs every migration
    at boot: 57 s, measured.
  */
  vyos =
    { version, hash }:
    let
      parts =
        runCommand "vyos-${version}-parts"
          {
            nativeBuildInputs = [
              xorriso
              squashfsTools
              python3
            ];
            iso = fetchurl {
              url = "https://github.com/vyos/vyos-nightly-build/releases/download/${version}/vyos-${version}-generic-amd64.iso";
              inherit hash;
            };
          }
          ''
            mkdir $out
            xorriso -osirrox on -indev $iso \
              -extract /live/filesystem.squashfs $out/filesystem.squashfs \
              -extract /live/vmlinuz $out/vmlinuz \
              -extract /live/initrd.img $out/initrd.img
            unsquashfs -cat $out/filesystem.squashfs \
              usr/lib/python3/dist-packages/vyos/xml_ref/cache.py > cache.py
            python3 -I ${./pkgs/vyos-footer.py} cache.py > $out/footer
          '';
    in
    configBoot: {
      format = "raw";
      kernel = "${parts}/vmlinuz";
      initrd = "${parts}/initrd.img";
      cmdline = "boot=live rootdelay=5 vyos-union=/boot/${version} console=ttyS0,115200 net.ifnames=0 biosdevname=0";
      # vyos-router's last line before the login prompt, which has no
      # newline: `vyos-router[1049]:  activate configure.`.
      ready = "vyos-router\\[[0-9]+\\]:.* configure\\.$";
      disk =
        runCommand "vyos-${version}-disk"
          {
            nativeBuildInputs = [
              e2fsprogs
              util-linux
              fakeroot
            ];
            passAsFile = [ "configBoot" ];
            inherit configBoot;
          }
          ''
            root=$PWD/root
            boot=$root/boot/${version}
            mkdir -p $boot/rw/opt/vyatta/etc/config $root/boot/grub
            cp ${parts}/filesystem.squashfs $boot/${version}.squashfs
            echo '/ union' > $root/persistence.conf
            { cat $configBootPath; cat ${parts}/footer; echo '// Release version: ${version}'; } \
              > $boot/rw/opt/vyatta/etc/config/config.boot
            size=$(( $(du -sm $root | cut -f1) + 128 ))
            truncate -s ''${size}M fs.img
            fakeroot sh -c "chown -R 0:0 $root && mkfs.ext4 -q -L persistence -d $root fs.img"
            truncate -s $(( size + 2 ))M $out
            printf 'label: gpt\nstart=2048, type=linux\n' | sfdisk -q $out
            dd if=fs.img of=$out bs=1M seek=1 conv=notrunc status=none
          '';
    };

  floating =
    url:
    let
      sums = builtins.readFile (builtins.fetchurl "${url}.sha256");
      hex = builtins.head (builtins.match "([0-9a-f]{64}) .*" sums);
    in
    fetchurl {
      inherit url;
      hash = "sha256:${hex}";
    };
in
{
  /**
    openSUSE Leap 16.0, the community build of SLE 16. Root is XFS on the
    third partition.

    Masked under UML, each measured on this image:
    - `jeos-firstboot`, an interactive wizard that loops on the console.
    - `kbdsettings`: UML has no keyboard, and `setleds` fails the unit.
  */
  opensuse-leap-16_0 = {
    disk = floating "https://download.opensuse.org/distribution/leap/16.0/appliances/Leap-16.0-Minimal-VM.x86_64-Cloud.qcow2";
    partition = 3;
    kernelParams = [
      "systemd.mask=jeos-firstboot.service"
      "systemd.mask=kbdsettings.service"
    ];
  };

  /**
    MikroTik RouterOS 7.24.5, Cloud Hosted Router, for `vivarium.appliance`.

    Boots in about 6 s under KVM with `admin` and an empty password, a
    DHCP client on ether1 (the uplink) and REST on port 80. Unlicensed,
    it limits each interface to 1 Mbit/s: enough to configure and route,
    not to measure. A pinned version, unlike the cloud images above:
    MikroTik keeps old releases.
  */
  routeros-chr-7_24_5 = {
    disk =
      runCommand "routeros-chr-7.24.5.img"
        {
          src = fetchurl {
            url = "https://download.mikrotik.com/routeros/7.24.5/chr-7.24.5.img.zip";
            hash = "sha256-FvByIqMhM1LJxPvqT03sao/7eP3A+l45jCMQPtNXxus=";
          };
          nativeBuildInputs = [ unzip ];
        }
        ''
          unzip -p $src chr-7.24.5.img > $out
        '';
    format = "raw";
    # The login prompt has no newline after it; the banner line before it does.
    ready = "MikroTik 7\\.24\\.5 ";
  };

  /**
    VyOS rolling 2026.10.26, for `vivarium.appliance`: a function of the
    `config.boot` text to boot with. Log in as `vyos`, with the password
    the config sets. Give each ethernet its `hw-id`, the MAC in
    `vivarium.nics`: VyOS leaves an interface without one unbound. VyOS
    publishes nightly ISOs on GitHub and prunes old releases, so this pin
    may stop downloading; the store keeps it.
  */
  vyos-2026_10_26 = vyos {
    version = "2026.10.26-0027-rolling";
    hash = "sha256-u5H/DWC/LK6BzLG/Izo6vP4Qe9Qv2b24fCQHVlVg/5Y=";
  };

  /**
    VyOS rolling, for `vivarium.vyos`: the root squashfs of the ISO.

    Pinned, not floating: VyOS publishes no checksum beside a fixed name.
    The nightly releases on GitHub are deleted after some weeks, so a
    pin goes stale and needs a bump.
  */
  vyos-rolling = {
    squashfs =
      let
        iso = fetchurl {
          url = "https://github.com/vyos/vyos-nightly-build/releases/download/2026.10.21-0025-rolling/vyos-2026.10.21-0025-rolling-generic-amd64.iso";
          hash = "sha256-xlLFEH7JyaGTrfKc3ZWdubhqJNVlfPpzQykeo1DDarQ=";
        };
      in
      # Local: on a remote builder the ISO goes out and the squashfs
      # comes back, 1.2 GB for a copy.
      runCommand "vyos-2026.10.21-0025-rolling.squashfs"
        {
          nativeBuildInputs = [ libarchive ];
          preferLocalBuild = true;
          allowSubstitutes = false;
        }
        ''
          bsdtar -xOf ${iso} live/filesystem.squashfs > $out
        '';
  };

  /**
    MikroTik RouterOS CHR 7.24.5, for `vivarium.routeros`: the raw disk
    image, 128 MiB. MikroTik keeps every version at its URL, so the pin
    holds. Unlicensed, each interface carries 1 Mbit/s.
  */
  routeros-chr = {
    disk =
      (fetchzip {
        url = "https://download.mikrotik.com/routeros/7.24.5/chr-7.24.5.img.zip";
        hash = "sha256-EoBfTQahvZ2PVMz/EwKKhbo6QACJtl3gnp9lyHMRC1U=";
        stripRoot = false;
      })
      + "/chr-7.24.5.img";
  };

  /**
    SONiC VS (virtual switch) 202505, for `vivarium.sonic`: the community
    build's own qcow2, 16 GiB virtual. Debian with the SONiC stack; boots
    its own kernel and GRUB, so QEMU runs it unchanged.

    Pinned to one Azure pipeline build. SONiC publishes no stable release
    URL: `sonic-build.azurewebsites.net` serves a build's artifacts under
    its numeric `buildId`, and a build ages out of the pipeline's
    retention, so the pin needs a bump then. Same floating-image problem
    as the cloud images above (nixidae/nixidae#19). To bump: open
    https://sonic-build.azurewebsites.net/ui/sonic/pipelines, platform
    `vs`, branch `202505`, newest succeeded build, and read its
    `target/sonic-vs.img.gz`.

    The artifact is a gzip'd 6 GiB qcow2. The store holds a recompressed
    qcow2 of 1.9 GiB instead: QEMU decompresses a zlib cluster on read,
    and the image is read-only under the per-run overlay. That thirds the
    store footprint and what CI substitutes, at ~3 min of `qemu-img` once.
  */
  sonic-vs = {
    disk =
      let
        gz = fetchurl {
          url = "https://sonic-build.azurewebsites.net/api/sonic/artifacts?branchName=202505&platform=vs&buildId=1238524&target=target%2Fsonic-vs.img.gz";
          hash = "sha256-tCJYBLIJhwTJnit8eQ/m9w688H+uVd7W/mMELFNcLug=";
          name = "sonic-vs-202505-1238524.img.gz";
        };
      in
      runCommand "sonic-vs-202505.qcow2"
        {
          nativeBuildInputs = [
            gzip
            qemu-utils
          ];
          preferLocalBuild = true;
          allowSubstitutes = false;
        }
        ''
          gzip -dc ${gz} > full.qcow2
          qemu-img convert -c -O qcow2 full.qcow2 $out
        '';
    format = "qcow2";
  };
}

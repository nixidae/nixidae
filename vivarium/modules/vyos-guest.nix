# A guest that boots VyOS under UML's kernel, from the squashfs of a
# VyOS ISO.
#
# VyOS boots through Debian's live-boot: its initramfs unions the
# squashfs with a persistence partition. Here a busybox initramfs does
# the same with a tmpfs upper, so writes are lost at poweroff. Before it
# hands over to VyOS's systemd it mounts the store and the artifacts over
# hostfs, enables the vivarium agent, and writes `config.boot`.
#
# As with `vivarium.image`, the node's NixOS system is never built.
{
  config,
  lib,
  pkgs,
  ...
}:
let
  cfg = config.vivarium.vyos;
  inherit (lib) mkOption types;

  runner = config.system.build.vivariumRunnerPackage;
  units = import ./foreign-units.nix { inherit lib pkgs runner; };
  # vec0 is the uplink. On the out-of-band fabric (nixidae/nixidae#958) it
  # joins a management segment and leases there, so rename it eth0 and let
  # VyOS DHCP it. Otherwise VyOS does not manage it: the agent is on ttyS0,
  # not the network. Its MAC is the runner's `spec.mac(0)`, lowercase.
  hex = n: (lib.optionalString (n < 16) "0") + lib.toHexString n;
  uplinkMac = lib.toLower "52:54:00:12:00:${hex config.vivarium.index}";
  onOob = config.vivarium.uplink.segment != null;
  uplinkLink = lib.optional onOob {
    name = "05-vivarium-eth0.link";
    path = pkgs.writeText "eth0.link" ''
      [Match]
      MACAddress=${uplinkMac}

      [Link]
      Name=eth0
    '';
  };

  # vivarium names an interface by its attribute on every backend. UML
  # names NICs vecN, which VyOS's own naming rule (65-vyos-net.rules,
  # eth*/wlan*/e* only) never touches, so a .link file renames each by
  # the MAC vivarium gave it.
  links = pkgs.linkFarm "vivarium-vyos-links" (
    uplinkLink
    ++ map (nic: {
      name = "10-vivarium-${nic.name}.link";
      path = pkgs.writeText "${nic.name}.link" ''
        [Match]
        MACAddress=${nic.mac}

        [Link]
        Name=${nic.name}
      '';
    }) config.vivarium.nics
  );

  # A management VRF for the uplink, like the RouterOS fabric test: eth0's
  # lease and routes land in their own table, isolated from the dataplane.
  uplinkVrf = if onOob then cfg.uplinkVrf else null;
  vrfTable = "100";
  vrfBlock = lib.optionalString (uplinkVrf != null) ''
    vrf {
        name ${uplinkVrf} {
            table "${vrfTable}"
        }
    }
  '';

  # eth0 leases its management address; a segment NIC takes the static
  # addresses the node gave it.
  uplinkStanza = lib.optionalString onOob ''
        ethernet eth0 {
            address "dhcp"
            hw-id "${uplinkMac}"${lib.optionalString (uplinkVrf != null) ''

            vrf "${uplinkVrf}"''}
        }
  '';

  # The interfaces' hw-id and addresses, in a block of their own after the
  # user's config.
  interfaceBlock = lib.optionalString (onOob || config.vivarium.nics != [ ]) ''
    interfaces {
    ${uplinkStanza}${lib.concatMapStrings (nic: ''
          ethernet ${nic.name} {
      ${lib.concatMapStrings (address: "        address \"${address}\"\n") nic.addresses}        hw-id "${nic.mac}"
          }
    '') config.vivarium.nics}}
  '';

  configBoot = pkgs.writeText "config.boot" (cfg.config + vrfBlock + interfaceBlock);
  roots = [
    runner
    units
    configBoot
    links
  ];

  # VyOS's own default, without its serial console: ttyS0 is the agent's
  # channel, and a getty there would read the agent's frames.
  defaultConfig = ''
    interfaces {
        loopback lo {
        }
    }
    system {
        host-name "${config.networking.hostName}"
        login {
            user vyos {
                authentication {
                    encrypted-password "$6$QxPS.uk6mfo$9QBSo8u1FkH16gMyAVhus6fU3LOzvLR9Z9.82m3tiHFAxTtIkhaZSWssSgzt4v4dGAL8rhVQxTg0oAG9/q11h/"
                    plaintext-password ""
                }
            }
        }
    }
  '';

  # The kernel hands parameters it does not know to /init as environment
  # variables: VIVARIUM_STORE and VIVARIUM_ARTIFACTS (see image.nix).
  # Store paths are read through /root/nix, the store mounted in the new
  # root, so the initramfs holds busybox and this script only.
  init = pkgs.writeScript "vyos-init" ''
    #!/bin/sh
    export PATH=/bin
    set -e
    # The lower directory's name says "live boot" to vyos-persistpath,
    # which is true: nothing persists. Under any other name it reads the
    # upper directory as <persistence>/boot/<image>/rw, and vyos-router
    # bind-mounts a /boot that does not exist.
    lower=/live/filesystem.squashfs
    mkdir -p /proc /dev $lower /rw /root
    mount -t proc none /proc
    mount -t devtmpfs none /dev

    mount -t squashfs -o ro /dev/ubda $lower
    mount -t tmpfs -o mode=0755 tmpfs /rw
    mkdir -p /rw/upper /rw/work
    mount -t overlay overlay -o lowerdir=$lower,upperdir=/rw/upper,workdir=/rw/work /root

    mkdir -p /root/nix /root/artifacts
    mount -t hostfs none /root/nix -o "$VIVARIUM_STORE"
    if [ -n "$VIVARIUM_ARTIFACTS" ]; then
      mount -t hostfs none /root/artifacts -o "$VIVARIUM_ARTIFACTS"
    fi

    system=/root/etc/systemd/system
    cp /root${units}/* $system/
    mkdir -p $system/multi-user.target.wants
    ln -sf ../vivarium-agent.service ../vivarium-journal.service $system/multi-user.target.wants/
    mkdir -p /root/etc/systemd/network
    for link in /root${links}/*; do
      if [ -e "$link" ]; then cp "$link" /root/etc/systemd/network/; fi
    done

    # vyos-router owns the file: it changes its group and mode, and the
    # migration rewrites it, so it is a copy and not the store path.
    boot=/root/opt/vyatta/etc/config/config.boot
    cp /root${configBoot} $boot
    if ! grep -q vyos-config-version $boot; then
      chroot /root /usr/bin/python3 /usr/libexec/vyos/add-system-version.py >> $boot
    fi

    umount /dev /proc
    exec switch_root /root /sbin/init
  '';

  initrd = pkgs.makeInitrd {
    compressor = "cat";
    contents = [
      {
        object = "${pkgs.pkgsStatic.busybox}/bin";
        symlink = "/bin";
      }
      {
        object = init;
        symlink = "/init";
      }
    ];
  };
in
{
  options.vivarium.vyos = mkOption {
    default = null;
    description = ''
      Boot VyOS instead of NixOS. UML backend only. Take one from
      `vivarium.images`, and give it a `config`.

      The guest runs the vivarium agent, so `succeed`, the journal stream
      and `/artifacts` work as on NixOS. `switch_to` and `add_closure` do
      not: there is no NixOS to switch, and no Nix database.
    '';
    type = types.nullOr (
      types.submodule {
        options = {
          squashfs = mkOption {
            type = types.path;
            description = "The root filesystem from a VyOS ISO, `live/filesystem.squashfs`.";
          };
          config = mkOption {
            type = types.lines;
            default = defaultConfig;
            defaultText = lib.literalMD "the loopback, the node's host name and the `vyos` user";
            description = ''
              `config.boot`, in VyOS's own syntax. VyOS migrates it at
              boot, and adds the version footer when it has none.

              Leave `system console` out: ttyS0 carries the agent.
            '';
          };
          kernelParams = mkOption {
            type = types.listOf types.str;
            default = [ ];
            example = [ "no-vyos-firewall" ];
          };
          uplinkVrf = mkOption {
            type = types.nullOr types.str;
            default = null;
            example = "mgmt";
            description = ''
              Put the out-of-band uplink (eth0) in this VRF, with a table
              of its own (nixidae/nixidae#958). Its lease and routes stay
              separate from the dataplane. Needs `vivarium.uplink.segment`.
            '';
          };
        };
      }
    );
  };

  config = lib.mkIf (cfg != null) {
    assertions = [
      {
        assertion = config.vivarium.backend == "uml";
        message = "vivarium.vyos boots under UML only; this guest is ${config.vivarium.backend}.";
      }
      {
        assertion = config.vivarium.image == null;
        message = "vivarium.vyos and vivarium.image both name the guest's root.";
      }
      {
        assertion = lib.all (nic: builtins.match "eth[0-9]+" nic.name != null) config.vivarium.nics;
        message = "VyOS configures ethernet interfaces named ethN only; name vivarium.interfaces eth1, eth2, ...";
      }
      {
        assertion = cfg.uplinkVrf != null -> config.vivarium.uplink.segment != null;
        message = "vivarium.vyos.uplinkVrf puts the uplink in a VRF, so it needs vivarium.uplink.segment.";
      }
    ];
    system.build.vivariumImageGuest = {
      initrd = "${initrd}/initrd";
      inherit (cfg) kernelParams;
    };
    system.build.vivariumImageDisk = cfg.squashfs;
    system.build.vivariumImageClosure = pkgs.closureInfo { rootPaths = roots; };
  };
}

/*
  VyOS booted with a config written in Nix, beside a NixOS peer.
  Needs /dev/kvm.
*/
{
  lib,
  vivarium,
  mkTest,
  ...
}:
mkTest {
  name = "vyos";
  phases.test = {
    script = ../vyos.py;
    after = [ "boot" ];
  };
  nodes.edge = { config, ... }: {
    vivarium.backend = "qemu";
    vivarium.memory = "1536M";
    imports = [ vivarium.kinds.appliance ];
    vivarium.appliance = vivarium.images.vyos-2026_10_26 ''
      firewall {
          ipv4 {
              input {
                  filter {
                      rule 10 {
                          action "drop"
                          protocol "icmp"
                          state "invalid"
                      }
                  }
              }
          }
      }
      interfaces {
          ethernet eth1 {
              hw-id "${(lib.head config.vivarium.nics).mac}"
              address "192.0.2.1/24"
              address "2001:db8::1/64"
          }
          loopback lo {
          }
      }
      system {
          console {
              device ttyS0 {
                  kernel
                  speed "115200"
              }
          }
          host-name "edge"
          login {
              user vyos {
                  authentication {
                      plaintext-password "vyos"
                  }
              }
          }
      }
    '';
    vivarium.interfaces.lan.segment = "lan";
  };
  nodes.peer.vivarium.interfaces.lan = {
    segment = "lan";
    addresses = [
      "192.0.2.2/24"
      "2001:db8::2/64"
    ];
  };
}

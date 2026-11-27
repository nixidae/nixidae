# `vivarium.needs`: what a guest needs from the host or its kernel, which
# the runner checks before any guest of the run boots
# (vivarium_runner/needs.py).
{
  config,
  hostPkgs,
  lib,
  ...
}:
let
  cfg = config.vivarium;
in
{
  # QEMU and libkrun stop at once without KVM (measured, nixidae/nixidae#1648:
  # "Could not access KVM kernel module", "Error creating the Kvm object").
  # A container guest uses the host's /dev/kvm for its nested guests.
  config.vivarium.needs.kvm =
    lib.mkIf
      (
        !hostPkgs.stdenv.hostPlatform.isDarwin
        && (
          lib.elem cfg.backend [
            "qemu"
            "krun"
          ]
          || (cfg.backend == "container" && cfg.nestedVirtualization)
        )
      )
      {
        on = "host";
        kind = "device";
        path = "/dev/kvm";
        why = "QEMU and libkrun run the guest on KVM";
        remedy = "load kvm_intel or kvm_amd on the host; in a Nix build, the daemon binds /dev/kvm into the sandbox of a derivation that asks for kvm";
      };

  options.vivarium.needs = lib.mkOption {
    type = lib.types.attrsOf (
      lib.types.submodule {
        options = {
          on = lib.mkOption {
            type = lib.types.enum [
              "host"
              "kernel"
            ];
            description = ''
              Who must have it. `host`: the machine that runs the guest,
              such as `/dev/kvm`. `kernel`: the kernel the guest runs on,
              which is the host's under the container backend; under UML,
              QEMU and krun it is the guest's own, and the runner does not
              check it.
            '';
          };
          kind = lib.mkOption {
            type = lib.types.enum [
              "device"
              "path"
            ];
            description = ''
              `device`: a device node the runner can read and write.
              `path`: a path that exists when the function is there, such
              as a sysctl a module adds. Name the function, not a module:
              a module built into the kernel can have no `/sys/module` entry.
              A module the kernel loads on use needs no entry: a link type,
              a queueing discipline (measured: `nlmon` and `sch_tbf` load
              from an unprivileged user namespace).
            '';
          };
          path = lib.mkOption {
            type = lib.types.strMatching "/.*";
            description = "The absolute path the runner looks at.";
          };
          why = lib.mkOption {
            type = lib.types.str;
            description = "What fails without it.";
          };
          remedy = lib.mkOption {
            type = lib.types.str;
            description = "What gives it to the host.";
          };
        };
      }
    );
    default = { };
    example = lib.literalExpression ''
      {
        br-netfilter = {
          on = "kernel";
          kind = "path";
          path = "/proc/sys/net/bridge/bridge-nf-call-iptables";
          why = "kube-proxy's rules never see traffic between pods on one node";
          remedy = "load br_netfilter on the host (modprobe br_netfilter)";
        };
      }
    '';
    description = ''
      What this guest needs from the host or from the kernel it runs on.
      The runner checks every guest's needs, templates included, before any
      guest boots, and refuses the run with each one missing, its reason
      and its remedy. A guest module declares its own.
    '';
  };
}

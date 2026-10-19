# NixOS checks an nftables ruleset at build time inside LKL, the kernel as
# a library (`networking.nftables.checkRuleset`). nixpkgs' lklWithFirewall
# asks for NFT_XFRM, but nothing in its defconfig selects the hidden XFRM
# that NFT_XFRM depends on, so Kconfig drops both. A rule with `meta
# secpath` or `ipsec in` then fails the build with "Operation not
# supported", although the guest kernel (pkgs/uml-kernel, ipsecConfig)
# runs it. XFRM_USER selects XFRM.
#
# Only on a guest with nftables: an overlay re-evaluates the package set,
# and no other guest has a use for it.
{ config, lib, ... }:
{
  config = lib.mkIf config.networking.nftables.enable {
    nixpkgs.overlays = [
      (_final: prev: {
        lklWithFirewall = prev.lklWithFirewall.overrideAttrs (old: {
          postPatch = old.postPatch + ''
            echo CONFIG_XFRM_USER=y >> arch/lkl/configs/defconfig
          '';
        });
      })
    ];
  };
}

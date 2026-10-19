# strongSwan to strongSwan on UML guests: a policy-based tunnel, and two
# route-based tunnels with eBGP over them, laid out like an AWS
# Site-to-Site VPN. README.md has the topology and what each phase shows.
#
#   nix build --file . ipsec
#   nix run --file . ipsec.driver -- --out ./out --break failover
{
  mkTest,
  lib,
  pkgs,
}:
let
  lab = import ./lab.nix {
    inherit lib pkgs;
    pki = pkgs.callPackage ./pki.nix { };
  };
in
mkTest {
  name = "ipsec";
  pythonPath = [ ./lib ];
  settings.lab = lab.settings;
  defaults = {
    vivarium.memory = "256M";
    # Real Ethernet, so the MTU phases measure what a WAN link does.
    vivarium.mtu = 1500;
    environment.systemPackages = [
      pkgs.tcpdump
      pkgs.nftables
      pkgs.socat
    ];
  };
  inherit (lab) nodes;
  phases = {
    pki = {
      script = ./phases/pki.py;
      after = [ "boot" ];
      description = "each gateway holds its certificate, the CA and a CRL listing serial 03";
    };
    policy = {
      script = ./phases/policy.py;
      after = [ "pki" ];
      description = "alice reaches bob through one CHILD_SA, ESP on the wire, nothing else let through";
    };
    route = {
      script = ./phases/route.py;
      after = [ "pki" ];
      description = "two XFRM tunnels, eBGP over each, carol reaches dave over both, the bogus prefix filtered";
    };
    mtu = {
      script = ./phases/mtu.py;
      after = [
        "policy"
        "route"
      ];
      description = "path MTU discovery through each kind of tunnel, to the byte";
    };
    pmtud = {
      script = ./phases/pmtud.py;
      after = [ "mtu" ];
      description = "DF copied to the outer header, the sender's cached path MTU, and where a packet without DF is fragmented";
    };
    blackhole = {
      script = ./phases/blackhole.py;
      after = [ "pmtud" ];
      description = "an underlay that drops large ESP without ICMP: TCP stalls, rt mtu clamping is not enough, MSS 1300 is";
    };
    udp = {
      script = ./phases/udp.py;
      after = [ "blackhole" ];
      description = "UDP without DF arrives fragmented; with DF it is lost once, then EMSGSIZE";
    };
    rekey = {
      script = ./phases/rekey.py;
      after = [ "udp" ];
      description = "CHILD_SA and IKE_SA rekeyed under load, no packet lost";
    };
    failover = {
      script = ./phases/failover.py;
      after = [ "rekey" ];
      description = "tunnel 1 cut: DPD and BGP move traffic to tunnel 2, then back to both";
    };
    impostor = {
      script = ./phases/impostor.py;
      after = [ "failover" ];
      description = "mallory as west: revoked key, rogue CA, weak proposal, spoofed plaintext; all refused";
    };
    lifecycle = {
      script = ./phases/lifecycle.py;
      after = [ "impostor" ];
      description = "west's certificate renewed under load; an expired certificate and a stale CRL each take the tunnel down";
    };
  };
}

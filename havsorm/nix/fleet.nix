/**
  A Salt master drives a fleet with havsorm: a NixOS minion and an
  openSUSE Leap minion, on vivarium.

  The master and the NixOS minion need compute, so they run on QEMU. SUSE
  boots its own cloud image, which runs on UML only. SUSE has no Salt
  package here, so it runs havsorm's Salt from the host's store.
*/
{
  pkgs,
  vivarium,
  saltEnv,
}:
let
  lab = n: {
    segment = "lab";
    addresses = [ "10.9.0.${toString n}/24" ];
  };
  minionConfig = id: {
    master = "10.9.0.1";
    inherit id;
  };
  srv = ../tests/fleet/srv;
in
vivarium.mkTest {
  name = "havsorm-fleet";
  settings.salt = {
    closure = "${pkgs.closureInfo { rootPaths = [ saltEnv ]; }}";
    bin = "${saltEnv}/bin";
    config = builtins.toJSON (minionConfig "suse1");
  };
  defaults = {
    imports = [ ./nixos.nix ];
    services.havsorm.package = saltEnv;
  };
  nodes.master = {
    vivarium.backend = "qemu";
    vivarium.memory = "1536M";
    vivarium.interfaces.lab = lab 1;
    networking.firewall.allowedTCPPorts = [
      4505
      4506
    ];
    services.salt.master = {
      enable = true;
      configuration = {
        auto_accept = true;
        file_roots.base = [ "${srv}" ];
        reactor = [ { "havsorm/release/*" = [ "${srv}/reactor/release.sls" ]; } ];
      };
    };
  };
  nodes.web1 = {
    vivarium.backend = "qemu";
    vivarium.memory = "1536M";
    vivarium.interfaces.lab = lab 2;
    services.salt.minion = {
      enable = true;
      configuration = minionConfig "web1";
    };
    environment.etc."havsorm-generation".text = "base\n";
    vivarium.configurations.next = {
      environment.etc."havsorm-generation".text = pkgs.lib.mkForce "next\n";
    };
  };
  nodes.suse1 = {
    vivarium.image = vivarium.images.opensuse-leap-16_0;
    vivarium.memory = "1024M";
    vivarium.interfaces.lab = lab 3;
  };
  phases.fleet = {
    script = ../tests/fleet/fleet.py;
    after = [ "boot" ];
  };
}

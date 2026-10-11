/**
  A Salt master drives a fleet with havsorm: a NixOS minion and an
  openSUSE Leap minion, on vivarium.

  The master and the NixOS minion need compute, so they run on QEMU. SUSE
  boots its own cloud image, which runs on UML only.

  The master bootstraps SUSE with salt-ssh, as Uyuni bootstraps a
  client: SSH as a sudo user, then a state that installs and starts the
  minion. SUSE has no Salt package here, so the state points the unit at
  havsorm's Salt in the host's store, which the script puts in SUSE's
  store view first: that stands in for a package repository.
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
  # A NixOS minion, with two more systems to deploy: `next`, and `broken`,
  # whose activation fails because a unit fails to start.
  web =
    n:
    { config, ... }:
    {
      vivarium.backend = "qemu";
      vivarium.memory = "1536M";
      vivarium.interfaces.lab = lab n;
      services.salt.minion = {
        enable = true;
        configuration = minionConfig config.networking.hostName;
      };
      environment.etc."havsorm-generation".text = "base\n";
      vivarium.configurations.next = {
        environment.etc."havsorm-generation".text = pkgs.lib.mkForce "next\n";
      };
      vivarium.configurations.broken = {
        environment.etc."havsorm-generation".text = pkgs.lib.mkForce "broken\n";
        systemd.services.havsorm-broken = {
          wantedBy = [ "multi-user.target" ];
          serviceConfig = {
            Type = "oneshot";
            ExecStart = "${pkgs.coreutils}/bin/false";
          };
        };
      };
    };
  key = pkgs.runCommand "salt-ssh-key" { nativeBuildInputs = [ pkgs.openssh ]; } ''
    mkdir $out
    ssh-keygen -q -t ed25519 -N "" -C salt-ssh -f $out/id_ed25519
  '';
in
vivarium.mkTest {
  name = "havsorm-fleet";
  settings.salt = {
    closure = "${pkgs.closureInfo { rootPaths = [ saltEnv ]; }}";
    env = "${saltEnv}";
    key = "${key}/id_ed25519";
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
    environment.systemPackages = [ pkgs.openssh ];
    # The script installs the key here with mode 0600: ssh refuses the
    # store's 0444 copy.
    environment.etc."salt/roster".text = builtins.toJSON {
      suse1 = {
        host = "10.9.0.3";
        user = "salt";
        sudo = true;
        priv = "/etc/salt/ssh/id_ed25519";
      };
    };
    services.salt.master = {
      enable = true;
      configuration = {
        auto_accept = true;
        file_roots.base = [ "${srv}" ];
        reactor = [ { "havsorm/release/*" = [ "${srv}/reactor/release.sls" ]; } ];
      };
    };
  };
  nodes.web1 = web 2;
  nodes.web2 = web 4;
  nodes.suse1 = {
    vivarium.image = vivarium.images.opensuse-leap-16_0 // {
      userData.users = [
        "default"
        {
          name = "salt";
          sudo = "ALL=(ALL) NOPASSWD:ALL";
          ssh_authorized_keys = [ (builtins.readFile "${key}/id_ed25519.pub") ];
        }
      ];
    };
    vivarium.memory = "1024M";
    vivarium.interfaces.lab = lab 3;
  };
  phases.fleet = {
    script = ../tests/fleet/fleet.py;
    after = [ "boot" ];
  };
}

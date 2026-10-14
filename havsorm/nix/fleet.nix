/**
  A Salt master drives a fleet with havsorm, on vivarium: two NixOS
  minions, an openSUSE Leap minion and a NixOS load balancer.

  The master and the NixOS web minions need compute, so they run on
  QEMU. SUSE boots its own cloud image, which runs on UML only, and the
  load balancer is UML too: it needs isolation, not compute.

  The master serves a signed binary cache. Every minion substitutes what
  havsorm deploys from it: Nix on SUSE, and the web and load balancer
  units everywhere. Those paths are in the master's store and in no
  minion's, so each deploy really substitutes.

  Each phase is one scenario, in order: bootstrap, nix-on-suse, nixos,
  rolling, service, heal. One script serves them all, by `vms.phase`.
*/
{
  pkgs,
  vivarium,
  saltEnv,
}:
let
  inherit (pkgs) lib;
  lab = n: {
    segment = "lab";
    addresses = [ "10.9.0.${toString n}/24" ];
  };
  cache = "http://10.9.0.1:5000";
  # A test key, in the repository: each minion's config names the public
  # half, and reading a generated one would be import from derivation.
  publicKey = lib.removeSuffix "\n" (builtins.readFile ../tests/fleet/cache-key/public);
  havsormConfig = {
    nix_settings = {
      substituters = cache;
      trusted-public-keys = publicKey;
    };
  };
  # Every minion publishes its lab address to the mine, and a web
  # minion says so in a grain: the load balancer reads both.
  minionConfig =
    id: roles:
    {
      master = "10.9.0.1";
      inherit id;
      havsorm = havsormConfig;
      mine_functions.lab_ip = [
        { mine_function = "network.ip_addrs"; }
        { interface = "lab"; }
      ];
    }
    // lib.optionalAttrs (roles != [ ]) { grains.roles = roles; };
  srv = ../tests/fleet/srv;
  # Not in any minion's store: `settings` carries them without string
  # context, and the master's cache serves them.
  unit =
    name: text:
    "${pkgs.writeTextDir name text}/${name}";
  webUnit = unit "havsorm-web.service" ''
    [Unit]
    Description=havsorm test web server
    [Service]
    ExecStart=${pkgs.darkhttpd}/bin/darkhttpd /var/lib/havsorm-web --port 8080
  '';
  lbUnit = unit "havsorm-lb.service" ''
    [Unit]
    Description=havsorm test load balancer
    [Service]
    ExecStart=${pkgs.haproxy}/bin/haproxy -Ws -f /etc/havsorm-lb.cfg
  '';
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
        configuration = minionConfig config.networking.hostName [ "web" ];
      };
      environment.etc."havsorm-generation".text = "base\n";
      vivarium.configurations.next = {
        environment.etc."havsorm-generation".text = lib.mkForce "next\n";
      };
      vivarium.configurations.broken = {
        environment.etc."havsorm-generation".text = lib.mkForce "broken\n";
        systemd.services.havsorm-broken = {
          wantedBy = [ "multi-user.target" ];
          serviceConfig = {
            Type = "oneshot";
            ExecStart = "${pkgs.coreutils}/bin/false";
          };
        };
      };
    };
  # The Nix SUSE installs: in the master's cache and nowhere SUSE can
  # see, so the install substitutes it.
  nixToInstall = pkgs.nix;
  key = pkgs.runCommand "salt-ssh-key" { nativeBuildInputs = [ pkgs.openssh ]; } ''
    mkdir $out
    ssh-keygen -q -t ed25519 -N "" -C salt-ssh -f $out/id_ed25519
  '';
  discard = builtins.unsafeDiscardStringContext;
  phase = after: nodes: {
    script = ../tests/fleet/fleet.py;
    inherit after nodes;
  };
in
vivarium.mkTest {
  name = "havsorm-fleet";
  settings.salt = {
    env = "${saltEnv}";
    key = "${key}/id_ed25519";
    # The bootstrap writes this into SUSE's minion config.
    config = minionConfig "suse1" [ "web" ] // {
      havsorm.nix_settings = havsormConfig.nix_settings // {
        build-users-group = "";
      };
    };
  };
  settings.install.nix = discard "${nixToInstall}";
  settings.service = {
    web = discard webUnit;
    lb = discard lbUnit;
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
      5000
    ];
    environment.systemPackages = [
      pkgs.openssh
      pkgs.curl
    ];
    services.harmonia.cache = {
      enable = true;
      signKeyPaths = [ ../tests/fleet/cache-key/secret ];
    };
    system.extraDependencies = [
      nixToInstall
      webUnit
      lbUnit
    ];
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
        reactor = [
          { "havsorm/release/*" = [ "${srv}/reactor/release.sls" ]; }
          { "salt/beacon/*/service/*" = [ "${srv}/reactor/heal.sls" ]; }
        ];
      };
    };
  };
  nodes.web1 = web 2;
  nodes.web2 = web 4;
  nodes.suse1 = {
    vivarium.image = vivarium.images.opensuse-leap-16_0 // {
      # Nix gets installed here, so /nix must be writable.
      writableStore = true;
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
  nodes.lb =
    { config, ... }:
    {
      vivarium.memory = "768M";
      vivarium.interfaces.lab = lab 5;
      services.salt.minion = {
        enable = true;
        configuration = minionConfig config.networking.hostName [ ];
      };
    };
  phases = {
    bootstrap = phase [ "boot" ] [ ];
    nix-on-suse = phase [ "bootstrap" ] [
      "master"
      "suse1"
    ];
    nixos = phase [ "bootstrap" ] [
      "master"
      "web1"
    ];
    rolling = phase [ "nixos" ] [
      "master"
      "web1"
      "web2"
    ];
    service = phase [ "nix-on-suse" "rolling" ] [ ];
    heal = phase [ "service" ] [
      "master"
      "suse1"
    ];
  };
}

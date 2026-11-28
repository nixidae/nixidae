/*
  Names, HTTPS and a way in for a lab of NixOS guests (nixidae/nixidae#1669).

  Every guest behind the run's router is `<name>.<domain>` for its host
  name and each of its `vivarium.lab.names`. The router's SOCKS proxy
  answers those names from a zone built here, with no DNS server, so
  `vivarium shell` and
  `vivarium browser` reach them from the host. Each guest's `/etc/hosts`
  holds them too. A name's `ports` add SRV records. A guest with
  `vivarium.lab.tls.enable` gets a certificate from the run's lab CA in
  the `lab` phase (../../recipes/lab.py).

  Kubernetes is one user of this: its recipe adds `cluster.local` to the
  same proxy. Template instances get their addresses at run time, so they
  have no names here.
*/
{
  config,
  lib,
  systems,
  ...
}:
let
  cfg = config.vivarium.recipes.lab;
  routerName = config.router.name;
  guests = lib.filterAttrs (
    name: system: name != routerName && system.config.vivarium.uplink.proxy == "router"
  ) systems;
  fqdn = name: "${name}.${cfg.domain}";
  address = system: system.config.vivarium.router.address system.config.vivarium.index;
  labOf = system: system.config.vivarium.lab;

  owners = lib.concatLists (
    lib.mapAttrsToList (
      guest: system:
      lib.mapAttrsToList (name: entry: {
        inherit guest name;
        inherit (entry) ports;
      }) (labOf system).names
    ) guests
  );
  hosts = lib.listToAttrs (
    map (owner: lib.nameValuePair (fqdn owner.name) [ (address guests.${owner.guest}) ]) owners
  );
  services = lib.listToAttrs (
    lib.concatMap (
      owner:
      lib.concatLists (
        lib.mapAttrsToList (
          protocol: port:
          lib.optional (port != null) (
            lib.nameValuePair "_${protocol}._tcp.${fqdn owner.name}" [
              {
                priority = 0;
                weight = 0;
                inherit port;
                target = fqdn owner.name;
              }
            ]
          )
        ) owner.ports
      )
    ) owners
  );
  taken = lib.filter (name: lib.count (owner: owner.name == name) owners > 1) (
    lib.unique (map (owner: owner.name) owners)
  );
  certified = lib.filterAttrs (_: system: (labOf system).tls.enable) guests;
in
{
  options.vivarium.recipes.lab = {
    enable = lib.mkEnableOption ''
      lab names, HTTPS and a way in from the host for the guests behind the
      run's router: `<guest>.<domain>` through the router's SOCKS proxy, SRV
      records for `vivarium.lab.names.<name>.ports`, and certificates for
      `vivarium.lab.tls`
    '';
    domain = lib.mkOption {
      type = lib.types.str;
      default = "lab.test";
      description = ''
        The domain the guests' names are under. It ends in `.test` or
        `.internal`: the lab CA signs for nothing else.
      '';
    };
  };

  config = lib.mkIf cfg.enable {
    assertions = [
      {
        assertion = lib.any (top: cfg.domain == top || lib.hasSuffix ".${top}" cfg.domain) [
          "test"
          "internal"
        ];
        message = "vivarium.recipes.lab.domain is ${cfg.domain}: the lab CA's name constraints refuse a certificate outside `test` and `internal`.";
      }
      {
        assertion = taken == [ ];
        message = "vivarium.lab.names: more than one guest is named ${lib.concatStringsSep ", " taken}.";
      }
    ];
    router.enable = lib.mkDefault true;
    nodes.${routerName}.vivarium.router.socks = {
      enable = lib.mkDefault true;
      zone = {
        domains = [ cfg.domain ];
        inherit hosts services;
      };
    };
    defaults.networking.hosts = lib.zipAttrs (
      lib.mapAttrsToList (name: found: { ${lib.head found} = name; }) hosts
    );
    settings.lab = {
      inherit (cfg) domain;
      # The `lab` phase waits for each: a guest runs before its lease.
      addresses = lib.mapAttrs (_: address) guests;
      tls = lib.mapAttrs (_: system: {
        names = map fqdn (lib.attrNames (labOf system).names);
        inherit ((labOf system).tls) directory group;
      }) certified;
    };
    phases.lab = {
      script = ../../recipes/lab.py;
      description = "each named guest holds its address, and each with `vivarium.lab.tls` its certificate";
      after = [ "boot" ];
    };
  };
}

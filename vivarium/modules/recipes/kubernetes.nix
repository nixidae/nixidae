# kubeadm nodes, the facts `vivarium_k8s.bring_up` needs about them, and
# `vivarium_k8s` in the run's venv.
#
# Every guest imports ../k8s.nix, which does nothing until a guest sets
# `services.vivarium-k8s.enable`. Those guests are the nodes. The recipe
# reads them from the evaluated guests and writes each one's CNI, image
# source and skipped addons to `settings.kubernetes.nodes`, so the cluster
# library asks the run, not the machine spec.
#
# `loadBalancer` puts the cluster behind the run's router: the router
# routes its Service range, gives LoadBalancer Services addresses
# (../k8s-lb.nix) and resolves `cluster.local` for its SOCKS5 proxy
# (nixidae/nixidae#1584).
#
# Several control planes sit behind the run's router too (`router.enable`):
# the router balances the API endpoint over them (../k8s-api.nix), and every node
# reaches the API there (`controlPlaneEndpoint`, nixidae/nixidae#1592).
#
# A template with `services.vivarium-k8s.enable` is a node added at run
# time: `vivarium_k8s.add_node` launches and joins one. Several control
# planes and `loadBalancer` take one only behind the router, as every
# other node is.
#
# `certManager` runs cert-manager with a ClusterIssuer, `vivarium`, on the
# run's intermediate CA (`vms.trust`), so the host trusts what it issues
# through `access/ca-bundle.pem`.
{
  config,
  lib,
  systems,
  templateNodes,
  vivariumPackages,
  ...
}:
let
  cfg = config.vivarium.recipes.kubernetes;
  routerName = config.router.name;
  nodes = lib.filterAttrs (
    name: system: name != routerName && system.config.services.vivarium-k8s.enable
  ) systems;
  templates = lib.filterAttrs (_: node: node.services.vivarium-k8s.enable) templateNodes;
  notRouted = lib.attrNames (
    lib.filterAttrs (_: node: node.vivarium.uplink.proxy != "router") templates
  );
  # What `vivarium_k8s` reads of one node: its `NodeFacts`.
  facts =
    node:
    lib.getAttrs [
      "cni"
      "images"
      "skipAddons"
      "role"
      "persist"
    ] node.services.vivarium-k8s;

  # The first node's view of the cluster's ranges: kubeadm takes them from
  # the control plane, and every node sets the same.
  some = (lib.head (lib.attrValues nodes)).config.services.vivarium-k8s;
  controlPlanes = lib.attrNames (
    lib.filterAttrs (_: system: system.config.services.vivarium-k8s.role == "control-plane") nodes
  );
  several = lib.length controlPlanes > 1;
  uplink = import ../../nix/uplink.nix;
  apiPort = 6443;
  # Each node's address on the router's network.
  behindRouter = lib.mapAttrs (
    _: system: system.config.vivarium.router.address system.config.vivarium.index
  ) nodes;

  prefixes = import ../../nix/prefixes.nix lib;
  # The pool, the cluster's ranges and the uplink: no two may share an
  # address, or the router routes one into another.
  ranges = [
    {
      name = "loadBalancer.pool";
      text = cfg.loadBalancer.pool;
    }
    {
      name = "serviceSubnet";
      text = some.serviceSubnet;
    }
    {
      name = "podSubnet";
      text = some.podSubnet;
    }
    {
      name = "the uplink";
      text = (import ../../nix/uplink.nix).subnet;
    }
  ];
  unparsedRanges = lib.filter (entry: prefixes.fromIPv4 entry.text == null) ranges;
  overlappingRanges = prefixes.overlaps (
    map (entry: entry // { prefix = prefixes.fromIPv4 entry.text; }) ranges
  );

  # The Service range's nth address: the API is the first, cluster DNS the
  # tenth, as kubeadm gives them.
  nthService =
    n:
    let
      octets = lib.splitString "." (lib.head (lib.splitString "/" some.serviceSubnet));
    in
    lib.concatStringsSep "." (lib.init octets ++ [ (toString (lib.toInt (lib.last octets) + n)) ]);
in
{
  options.vivarium.recipes.kubernetes = {
    enable = lib.mkEnableOption "kubeadm nodes: every guest with `services.vivarium-k8s.enable`";
    loadBalancer = {
      enable = lib.mkEnableOption ''
        LoadBalancer Services, and the cluster reached from the host by name.
        The run gets a router: it routes the Service range, gives each
        LoadBalancer Service an address from `pool` and serves a SOCKS5
        proxy that resolves `cluster.local`
      '';
      pool = lib.mkOption {
        type = lib.types.str;
        default = "10.200.0.0/24";
        description = "The addresses LoadBalancer Services get. Apart from the Pod and Service ranges.";
      };
    };
    certManager.enable = lib.mkEnableOption ''
      cert-manager, and a ClusterIssuer `vivarium` that signs with the run's
      intermediate CA. The host trusts its certificates through
      `access/ca.pem` or `access/ca-bundle.pem`
    '';
  };

  config = lib.mkIf cfg.enable (
    lib.mkMerge [
      {
        defaults.imports = [ ../k8s.nix ];
        python.packages = [ vivariumPackages.k8s ];

        # One definition: `settings` does not merge two of `kubernetes`.
        settings.kubernetes = {
          nodes = lib.mapAttrs (_: system: facts system.config) nodes;
          # Always present: whether it is empty needs the templates evaluated.
          templates = lib.mapAttrs (_: facts) templates;
        }
        // lib.optionalAttrs several { api.router = routerName; }
        // lib.optionalAttrs cfg.loadBalancer.enable { loadBalancer.router = routerName; }
        // lib.optionalAttrs cfg.certManager.enable {
          certManager = {
            manifest = "${vivariumPackages.certManager.manifest}";
            issuer = "vivarium";
          };
        };
      }
      (lib.mkIf cfg.certManager.enable {
        # Every node: the scheduler may put a pod on any of them. Not
        # `nodes.<name>`: which guests are nodes is read from them.
        defaults =
          { config, ... }:
          lib.mkIf config.services.vivarium-k8s.enable {
            services.vivarium-k8s.extraImages = [ vivariumPackages.certManager.tarball ];
            # What the images point at, and what `bring_up` applies.
            vivarium.nixDatabase.extraRoots = map (path: "${path}") (
              lib.attrValues vivariumPackages.certManager.binaries ++ [ vivariumPackages.certManager.manifest ]
            );
          };
      })
      {
        assertions = [
          {
            assertion = !several || config.router.enable == true;
            message = "vivarium.recipes.kubernetes: several control planes (${lib.concatStringsSep ", " controlPlanes}) sit behind the run's router: set router.enable.";
          }
          {
            assertion = notRouted == [ ] || !(several || cfg.loadBalancer.enable);
            message = "vivarium.recipes.kubernetes: the instances of ${lib.concatStringsSep ", " notRouted} do not sit behind the run's router, so several control planes and loadBalancer cannot take one. Leave their vivarium.uplink.proxy to the router.";
          }
        ];
      }
      # On `router.enable`, not on `several`: which guests are control
      # planes is read from the guests these options make. `true`, not
      # the run's own decision on `null`, which reads the guests too.
      (lib.mkIf (config.router.enable == true) {
        defaults =
          { config, ... }:
          lib.mkIf config.services.vivarium-k8s.enable {
            services.vivarium-k8s.controlPlaneEndpoint = lib.mkIf several "${uplink.services}:${toString apiPort}";
          };
        nodes.${routerName} = {
          imports = [ ../k8s-api.nix ];
          vivarium.k8sApi = lib.mkIf several {
            enable = true;
            port = apiPort;
            controlPlanes = lib.genAttrs controlPlanes (name: "${behindRouter.${name}}:${toString apiPort}");
          };
        };
      })
      (lib.mkIf cfg.loadBalancer.enable {
        assertions = [
          {
            assertion = unparsedRanges == [ ];
            message = "vivarium.recipes.kubernetes.loadBalancer: ${
              lib.concatMapStringsSep ", " (entry: "${entry.name} (${entry.text})") unparsedRanges
            } is no IPv4 prefix such as 10.200.0.0/24.";
          }
          {
            assertion = unparsedRanges != [ ] || overlappingRanges == [ ];
            message = "vivarium.recipes.kubernetes.loadBalancer: these overlap: ${
              lib.concatMapStringsSep "; " (
                pair: lib.concatMapStringsSep " and " (entry: "${entry.name} (${entry.text})") pair
              ) overlappingRanges
            }.";
          }
        ];
        # The router, its proxy and the lab's names are the generic lab's;
        # Kubernetes adds `cluster.local` and the LoadBalancer controller.
        vivarium.recipes.lab.enable = lib.mkDefault true;
        nodes.${routerName} = {
          imports = [ ../k8s-lb.nix ];
          _module.args.vivariumLb = vivariumPackages.lb;
          vivarium.k8sLoadBalancer = {
            enable = true;
            inherit (cfg.loadBalancer) pool;
            apiAddress = nthService 1;
          };
          vivarium.router = {
            routes = [
              {
                destination = some.serviceSubnet;
                via = lib.head controlPlanes;
              }
            ];
            # Pod ranges are routed per node at run time, by vivarium-lb.
            inside = [ some.podSubnet ];
            socks.domains."cluster.local" = [ (nthService 10) ];
          };
        };
      })
    ]
  );
}

/*
  The router's half of a cluster's LoadBalancer Services
  (nixidae/nixidae#1584): `vivarium-lb` and the routes it needs.
  `vivarium.recipes.kubernetes.loadBalancer` imports this into the router
  and sets the options; a router without them runs nothing of it.

  The controller starts when its credentials arrive:
  `vivarium_k8s.connect_load_balancer` writes a ServiceAccount's token and
  CA to `credentials`, after the cluster is up.
*/
{
  config,
  lib,
  pkgs,
  vivariumLb,
  ...
}:
let
  cfg = config.vivarium.k8sLoadBalancer;
  inherit (config.vivarium.router) vrf;
  inherit (lib) mkOption types;

  configFile = pkgs.writeText "vivarium-lb.json" (
    builtins.toJSON {
      serviceaccount = cfg.credentials;
      inherit (cfg) pool loadBalancerClass;
      inherit (vrf) table;
      ip = "${pkgs.iproute2}/bin/ip";
    }
  );
in
{
  options.vivarium.k8sLoadBalancer = {
    enable = lib.mkEnableOption "the cluster's LoadBalancer controller on this router";
    pool = mkOption {
      type = types.str;
      example = "10.200.0.0/24";
      description = "The addresses LoadBalancer Services get.";
    };
    apiAddress = mkOption {
      type = types.str;
      example = "10.96.0.1";
      description = "The API server as the router reaches it: the `kubernetes` Service's address.";
    };
    loadBalancerClass = mkOption {
      type = types.str;
      default = "vivarium.dev/router";
      description = "Services of no class, and of this one, are the controller's.";
    };
    credentials = mkOption {
      type = types.str;
      default = "/run/vivarium-lb/serviceaccount";
      readOnly = true;
      description = "Where the controller's token, `ca.crt` and `namespace` arrive.";
    };
  };

  config = lib.mkIf cfg.enable {
    assertions = [
      {
        assertion = config.vivarium.router.enable;
        message = "vivarium.k8sLoadBalancer runs on the run's router.";
      }
    ];
    vivarium.router.inside = [ cfg.pool ];

    systemd.paths.vivarium-lb = {
      wantedBy = [ "multi-user.target" ];
      pathConfig.PathExists = "${cfg.credentials}/token";
    };
    systemd.services.vivarium-lb = {
      description = "LoadBalancer Services for the lab's cluster";
      after = [ "systemd-networkd.service" ];
      # kr8s's in-cluster form: the server from these, the CA and token
      # from `credentials`.
      environment = {
        KUBERNETES_SERVICE_HOST = cfg.apiAddress;
        KUBERNETES_SERVICE_PORT = "443";
      };
      serviceConfig = {
        # In the uplink's VRF: the API's address and the nodes are its.
        ExecStart = "${pkgs.iproute2}/bin/ip vrf exec ${vrf.device} ${lib.getExe vivariumLb} ${configFile}";
        Restart = "always";
        RestartSec = 2;
      };
    };
  };
}

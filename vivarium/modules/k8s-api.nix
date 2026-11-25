/*
  The cluster's API endpoint on the run's router, for a cluster with
  several control planes (nixidae/nixidae#1592). `controlPlaneEndpoint`
  names the router's `services` address (nix/uplink.nix), and haproxy there
  passes each connection to a control plane whose `/readyz` answers. A
  control plane that stops answering gets no new connection.

  `vivarium.recipes.kubernetes` imports this into the router and sets the
  options; a router without them runs nothing of it.
*/
{
  config,
  lib,
  pkgs,
  ...
}:
let
  cfg = config.vivarium.k8sApi;
  inherit (config.vivarium.router) vrf;
  inherit (lib) mkOption types;

  configFile = pkgs.writeText "haproxy.cfg" ''
    global
      maxconn 1024

    defaults
      mode tcp
      timeout connect 5s
      # Watches and `kubectl exec` hold a connection open for long.
      timeout client 1h
      timeout server 1h
      # A refused connection, before the next check marks the server
      # down, goes to another control plane.
      retries 3
      option redispatch

    frontend api
      # Every address of the VRF: the guests ask on `services`, the host's
      # forward arrives on the gateway.
      bind :${toString cfg.port}
      default_backend control-planes

    backend control-planes
      option httpchk GET /readyz
      http-check expect status 200
      default-server check check-ssl verify none inter 2s fall 2 rise 1
    ${lib.concatStrings (
      lib.mapAttrsToList (name: address: "  server ${name} ${address}\n") cfg.controlPlanes
    )}'';
in
{
  options.vivarium.k8sApi = {
    enable = lib.mkEnableOption "the cluster's API endpoint on this router";
    port = mkOption {
      type = types.port;
      default = 6443;
      description = "The endpoint's port: `services.vivarium-k8s.controlPlaneEndpoint` names it.";
    };
    controlPlanes = mkOption {
      type = types.attrsOf types.str;
      example = {
        cp1 = "10.0.2.15:6443";
      };
      description = "Each control plane's API server as the router reaches it, by node name.";
    };
  };

  config = lib.mkIf cfg.enable {
    assertions = [
      {
        assertion = config.vivarium.router.enable;
        message = "vivarium.k8sApi runs on the run's router.";
      }
    ];
    vivarium.router.forwards = [ cfg.port ];

    systemd.services.vivarium-k8s-api = {
      description = "The cluster's API endpoint";
      wantedBy = [ "multi-user.target" ];
      after = [ "systemd-networkd.service" ];
      serviceConfig = {
        # In the uplink's VRF: the endpoint and the control planes are its.
        ExecStart = "${pkgs.iproute2}/bin/ip vrf exec ${vrf.device} ${lib.getExe' pkgs.haproxy "haproxy"} -db -f ${configFile}";
        # It binds in the VRF, which networkd may not have made yet.
        Restart = "always";
        RestartSec = 2;
      };
    };
  };
}

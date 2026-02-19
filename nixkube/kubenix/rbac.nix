# SPDX-License-Identifier: MIT

{
  config,
  lib,
  mkNCSI,
  ...
}:
let
  cfg = config.nix-csi;
in
{
  config = lib.mkIf cfg.enable {
    kubernetes.resources.none = {
      ClusterRole.nix-csi = {
        metadata.labels = cfg.labels;
        rules = [
          {
            apiGroups = [ "" ];
            resources = [ "pods" ];
            verbs = [
              "get"
              "list"
            ];
          }
          # Report events using events.k8s.io/v1 API across all namespaces
          {
            apiGroups = [ "events.k8s.io" ];
            resources = [ "events" ];
            verbs = [
              "get"
              "list"
              "create"
              "patch"
            ];
          }
        ];
      };
      ClusterRoleBinding.nix-csi = {
        metadata.labels = cfg.labels;
        subjects = lib.mkNamedList {
          nix-csi = {
            kind = "ServiceAccount";
            namespace = cfg.namespace;
          };
        };
        roleRef = {
          kind = "ClusterRole";
          name = "nix-csi";
          apiGroup = "rbac.authorization.k8s.io";
        };
      };
    };
    kubernetes.resources.${cfg.namespace} = {
      ServiceAccount.nix-csi = {
        metadata.labels = cfg.labels;
      };

      Role.nix-csi = {
        metadata.labels = cfg.labels;
        rules = [
          # Cache maintains up2date /etc/machines
          {
            apiGroups = [ "" ];
            resources = [ "pods" ];
            verbs = [
              "get"
              "list"
              "watch"
            ];
          }
          # ssh secret, CRUD
          {
            apiGroups = [ "" ];
            resources = [
              "secrets"
              "configmaps"
            ];
            verbs = [
              "get"
              "list"
              "create"
              "patch"
              "delete"
            ];
          }
        ];
      };

      # Binds the Role to the ServiceAccount.
      RoleBinding.nix-csi = {
        metadata.labels = cfg.labels;
        subjects = lib.mkNamedList {
          nix-csi.kind = "ServiceAccount";
        };
        roleRef = {
          kind = "Role";
          name = "nix-csi";
          apiGroup = "rbac.authorization.k8s.io";
        };
      };
    };
  };
}

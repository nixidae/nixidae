# SPDX-License-Identifier: MIT

{
  config,
  lib,
  ...
}:
let
  cfg = config.nixkube;
in
{
  config = lib.mkIf cfg.enable {
    kubernetes.resources.none = {
      ClusterRole.nixkube = {
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
          # Kubelet configz via API server proxy for CRI socket discovery
          {
            apiGroups = [ "" ];
            resources = [ "nodes/proxy" ];
            verbs = [ "get" ];
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
      ClusterRoleBinding.nixkube = {
        metadata.labels = cfg.labels;
        subjects = lib.mkNamedList {
          nixkube = {
            kind = "ServiceAccount";
            namespace = cfg.namespace;
          };
        };
        roleRef = {
          kind = "ClusterRole";
          name = "nixkube";
          apiGroup = "rbac.authorization.k8s.io";
        };
      };
    };
    kubernetes.resources.${cfg.namespace} = {
      ServiceAccount.nixkube = {
        metadata.labels = cfg.labels;
      };

      Role.nixkube = {
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
      RoleBinding.nixkube = {
        metadata.labels = cfg.labels;
        subjects = lib.mkNamedList {
          nixkube.kind = "ServiceAccount";
        };
        roleRef = {
          kind = "Role";
          name = "nixkube";
          apiGroup = "rbac.authorization.k8s.io";
        };
      };
    };
  };
}

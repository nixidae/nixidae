{ config, lib, ... }:
let
  cfg = config.nix-csi;
in
{
  config = lib.mkIf cfg.enable {
    kubernetes.resources.${cfg.namespace} = {
      ServiceAccount.nix-csi = { };

      Role.nix-csi = {
        rules = [
          # Cache maintains up2date /etc/machines
          {
            apiGroups = [ "" ];
            resources = [
              "nodes"
              "pods"
            ];
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
            ];
            verbs = [
              "get"
              "list"
              "create"
              "patch"
            ];
          }
          # Read authorized-keys
          {
            apiGroups = [ "" ];
            resources = [
              "configmaps"
            ];
            verbs = [
              "get"
              "list"
            ];
          }
        ];
      };

      # Binds the Role to the ServiceAccount.
      RoleBinding.nix-csi = {
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

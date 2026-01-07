{ ... }:
{
  imports = [
    ./options.nix
    ./namespace.nix
    ./daemonset.nix
    ./csidriver.nix
    ./config.nix
    ./cache.nix
    ./builder.nix
    ./rbac.nix
    ./undeploy.nix
    ./secret.nix
  ];
}

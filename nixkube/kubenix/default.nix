# SPDX-License-Identifier: MIT

{ ... }:
{
  imports = [
    ./options.nix
    ./namespace.nix
    ./daemonset.nix
    ./csidriver.nix
    ./config.nix
    ./pynixd.nix
    ./builder.nix

    ./rbac.nix
    ./undeploy.nix
    ./secret.nix
  ];
}

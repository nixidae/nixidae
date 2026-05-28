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
  config = {
    nixkube.loggingConfig = {
      renderer = "json";
      loggers = {
        nixkube.level = "DEBUG";
        httpx.level = "WARNING";
      };
      root.level = "INFO";
    };
    # Shared substituters for all CI variants (Kind and NixOS test VM).
    # NixOS test VM has nix-serve at 10.113.37.1:5000 (PTP CNI gateway).
    # Nix handles dead/unreachable substituters gracefully so this is safe on Kind.
    nixkube.node.nixConfig.settings.substituters = [
      "https://nix-csi.cachix.org"
      "https://cache.nixos.org"
      "http://10.113.37.1:5000?trusted=1"
    ];
  };
}

# SPDX-License-Identifier: MIT

{
  config,
  lib,
  ...
}:
let
  cfg = config.nixkube;
  namespace = cfg.namespace;
in
{
  config = lib.mkIf cfg.enable {
    kubernetes.resources.none.Namespace.${namespace} = {
      metadata.labels = cfg.labels;
    };
  };
}

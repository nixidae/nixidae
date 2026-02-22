# SPDX-License-Identifier: MIT

let
  system = builtins.currentSystem;
  inputs = (import ../. { }).inputs;

  pkgs = import inputs.nixpkgs { inherit system; };

  # You can use flakes, npins, niv, fetchTree, fetchFromGitHub or whatever.
  ekn = import inputs.easykubenix {
    inherit pkgs;
    modules = [
      (
        { config, lib, ... }:
        {
          kluctl = {
            discriminator = "nixtest";
            preDeployScript = # bash
              ''
                nix copy \
                  --substitute-on-destination \
                  --no-check-sigs \
                  --to ssh-ng://nix@nixcache.lillecarl.com?port=2222 \
                  ${config.kluctl.projectDir} \
                  -v || true
              '';
          };
          # Will go into the default namespace
          kubernetes.resources.nix-csi.Pod.nritest = {
            spec = {
              containers = lib.mkNamedList {
                nritest = {
                  image = "busybox:latest";
                  env = [
                    {
                      name = "AFILE";
                      value = pkgs.writeText "afile" "this is a file";
                    }
                  ];
                  command = [
                    "sleep"
                    "infinity"
                  ];
                };
              };
            };
          };
        }
      )
    ];
  };
in
ekn

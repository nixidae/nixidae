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
            metadata.annotations = {
              "nix-nri/pod-ssl" = "dir:/etc/ssl/certs=${pkgs.dockerTools.caCertificates}/etc/ssl/certs";
              "nix-nri/pod-group" = "file:/etc/group=${pkgs.dockerTools.fakeNss}/etc/group";
              "nix-nri/pod-passwd" = "file:/etc/passwd=${pkgs.dockerTools.fakeNss}/etc/passwd";
              "nix-nri/pod-nsswitch" = "file:/etc/nsswitch.conf=${pkgs.dockerTools.fakeNss}/etc/nsswitch.conf";
              "nix-nri/pod-binsh" = "file:/bin/sh=${pkgs.dockerTools.binSh}/bin/sh";
              "nix-nri/pod-usrbinenv" = "file:/usr/bin/env=${pkgs.dockerTools.usrBinEnv}/usr/bin/env";
            };
            spec = {
              containers = lib.mkNamedList {
                ${toString builtins.currentTime} = {
                  # image = "gcr.io/distroless/static:latest";
                  image = "ghcr.io/lillecarl/nix-csi/scratch:1.0.1";

                  env = lib.mkNamedList {
                    AFILE.value = pkgs.writeText "afile" "this is a file";
                    PATH.value = lib.makeBinPath [
                      pkgs.bash
                      pkgs.coreutils
                    ];
                  };
                  command = [
                    (lib.getExe pkgs.tini)
                    "--"
                    (lib.getExe' pkgs.coreutils "sleep")
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

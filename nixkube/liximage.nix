# SPDX-License-Identifier: MIT

{
  pkgs,
  lib,
}:
rec {
  server = "ghcr.io";
  repo = "${server}/lillecarl/nix-csi";

  images = lib.genAttrs [ "aarch64-linux" "x86_64-linux" ] (
    system:
    let
      sysPkgs = import pkgs.path {
        inherit system;
        overlays = [
          (import ./pkgs)
        ];
      };
      inherit (sysPkgs) lib;

      fakeNss = sysPkgs.dockerTools.fakeNss.override {
        extraGroupLines = [
          "nixbld:x:30000:"
        ];
      };
      runtimeInputs = [
        sysPkgs.coreutils
        sysPkgs.gitMinimal
        sysPkgs.lruLix
        sysPkgs.rsync
        sysPkgs.openssh
        sysPkgs.kubectl
      ];

      # TODO: consolidate init-copy and init-secrets into the same file (not same script)
      # ConfigMap or OCI? I think ConfigMap?
      init-copy = sysPkgs.writeShellApplication {
        name = "init-copy";
        inherit runtimeInputs;
        text = # bash
          ''
            set -euo pipefail
            set -x
            mkdir /tmp
            rsync --archive ${fakeNss}/ /

            # Check if we can SSH to nix-cache
            EXTRA_SUBSTITUTERS="local?trusted=true"
            if nix store ping --store ssh-ng://nix@nix-cache; then
              EXTRA_SUBSTITUTERS="$EXTRA_SUBSTITUTERS ssh-ng://nix@nix-cache?trusted=true"
            fi

            nix \
              build \
                --extra-substituters "$EXTRA_SUBSTITUTERS" \
                --max-jobs auto \
                --option sandbox false \
                --store /nix-volume \
                --out-link /nix-volume/nix/var/result \
                --fallback \
                "''${!ARCH}"
          '';
      };

      init-secrets = sysPkgs.writeShellApplication {
        name = "init-secrets";
        inherit runtimeInputs;
        text = # bash
          ''
            set -euo pipefail
            set -x
            mkdir /tmp
            rsync --archive ${fakeNss}/ /
            # shellcheck source=/dev/null
            source /opt/bin/init-secrets
          '';
      };

    in
    pkgs.dockerTools.streamLayeredImage {
      name = "${repo}/lix";
      tag = "${sysPkgs.lruLix.version}-${sysPkgs.stdenv.hostPlatform.system}";
      architecture = sysPkgs.go.GOARCH;

      maxLayers = 125;
      includeNixDB = true;
      contents = [
        sysPkgs.dockerTools.binSh
        sysPkgs.dockerTools.caCertificates
        sysPkgs.dockerTools.usrBinEnv
      ];
      config = {
        Entrypoint = [ (lib.getExe init-copy) ];
        Env = [
          "PATH=${
            lib.makeBinPath [
              init-copy
              init-secrets
            ]
          }"
          "ARCH=${sysPkgs.go.GOARCH}"
        ];
      };
    }
  );
  push =
    let
      copyToRegistry =
        arch:
        "${images.${arch}} | gzip --fast | skopeo copy docker-archive:/dev/stdin docker://${imageRef arch}";
      imageRef = arch: "${images.${arch}.imageName}:${images.${arch}.imageTag}"; # AI: imageName and imageTag exists
    in
    pkgs.writeShellApplication {
      name = "push";
      runtimeInputs = [
        pkgs.regctl
        pkgs.skopeo
        pkgs.gzip
        pkgs.cachix
      ];
      text = # bash
        ''
          skopeo login -u="$REPO_USERNAME" -p="$REPO_TOKEN" ${server}
          regctl registry login -u="$REPO_USERNAME" -p="$REPO_TOKEN" ${server}
          ${copyToRegistry "aarch64-linux"}
          ${copyToRegistry "x86_64-linux"}
          cachix push nix-csi ${images."aarch64-linux"}
          cachix push nix-csi ${images."x86_64-linux"}
          regctl index create ${repo}/lix:${pkgs.lruLix.version} \
            --ref ${imageRef "aarch64-linux"} \
            --ref ${imageRef "x86_64-linux"}
          regctl index create ${repo}/lix:latest \
            --ref ${imageRef "aarch64-linux"} \
            --ref ${imageRef "x86_64-linux"}
        '';
    };
}

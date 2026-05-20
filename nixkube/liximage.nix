# SPDX-License-Identifier: MIT

{
  pkgs,
  lib,
  nixkubeVersion ?
    (builtins.fromTOML (builtins.readFile ./pkgs/nixkube/pyproject.toml)).project.version,
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

            # Resolve the correct store path for this architecture from the JSON NODE_ENV
            STORE_PATH=$(nix eval --store dummy:// --raw --impure --expr \
              '(builtins.fromJSON (builtins.getEnv "NODE_ENV")).${system}')

            # Check if we can SSH to pynixd
            EXTRA_SUBSTITUTERS="local?trusted=true"
            if nix store ping --store ssh-ng://nix@pynixd; then
              EXTRA_SUBSTITUTERS="$EXTRA_SUBSTITUTERS ssh-ng://nix@pynixd?trusted=true"
            fi

            nix \
              build \
                --extra-substituters "$EXTRA_SUBSTITUTERS" \
                --max-jobs auto \
                --option sandbox false \
                --store /nix-volume \
                --out-link /nix-volume/nix/var/result \
                --fallback \
                "$STORE_PATH"
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
      tag = "${sysPkgs.lruLix.version}-${nixkubeVersion}-${sysPkgs.stdenv.hostPlatform.system}";
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
        ];
      };
    }
  );

  imageRef = system: "${images.${system}.imageName}:${images.${system}.imageTag}";

  # Per-arch push scripts (one per system, used by per-arch CI jobs)
  pushArch = lib.mapAttrs (
    system: image:
    pkgs.writeShellApplication {
      name = "push-lix-${system}";
      runtimeInputs = [
        pkgs.skopeo
        pkgs.gzip
        pkgs.cachix
      ];
      text = # bash
        ''
          skopeo login -u="$REPO_USERNAME" -p="$REPO_TOKEN" ${server}
          ${image} | gzip --fast | skopeo copy docker-archive:/dev/stdin docker://${imageRef system}
          cachix push nix-csi ${image}
        '';
    }
  ) images;

  # Manifest creation (used by dependent CI job after both arches are pushed)
  pushManifest = pkgs.writeShellApplication {
    name = "push-lix-manifest";
    runtimeInputs = [ pkgs.regctl ];
    text = # bash
      ''
        regctl registry login -u="$REPO_USERNAME" -p="$REPO_TOKEN" ${server}
        regctl index create ${repo}/lix:${pkgs.lruLix.version}-${nixkubeVersion} \
          --ref ${imageRef "aarch64-linux"} \
          --ref ${imageRef "x86_64-linux"}
        regctl index create ${repo}/lix:latest \
          --ref ${imageRef "aarch64-linux"} \
          --ref ${imageRef "x86_64-linux"}
      '';
  };
}

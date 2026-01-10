let
  inputs =
    (
      let
        lockFile = builtins.readFile ./flake.lock;
        lockAttrs = builtins.fromJSON lockFile;
        fcLockInfo = lockAttrs.nodes.flake-compatish.locked;
        fcSrc = builtins.fetchTree fcLockInfo;
        flake-compatish = import fcSrc;
      in
      flake-compatish ./.
    ).inputs;
  pkgs = import inputs.nixpkgs {
    overlays = [
      (import ./pkgs)
    ];
  };
  inherit (pkgs) lib;
  server = "ghcr.io";
  repo = "${server}/lillecarl/nix-csi";
in
rec {
  inherit inputs pkgs;
  images = lib.genAttrs [ "aarch64-linux" "x86_64-linux" ] (
    system:
    let
      sysPkgs = import inputs.nixpkgs { inherit system; };
      inherit (sysPkgs) lib;

      fakeNss = sysPkgs.dockerTools.fakeNss.override {
        extraGroupLines = [
          "nixbld:x:30000:"
        ];
      };
      runtimeInputs = [
        sysPkgs.coreutils
        sysPkgs.gitMinimal
        sysPkgs.lixPackageSets.lix_2_93.lix
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
            ARCH=$(nix eval --raw --impure --expr builtins.currentSystem)
            export ARCH
            case "$ARCH" in
              "x86_64-linux")
                export ARCH=amd64
              ;;
              "aarch64-linux")
                export ARCH=arm64
              ;;
            esac
            nix \
              build \
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
      tag = "${sysPkgs.lixPackageSets.lix_2_93.lix.version}-${sysPkgs.stdenv.hostPlatform.system}";
      architecture =
        {
          "aarch64-linux" = "arm64";
          "x86_64-linux" = "amd64";
        }
        .${sysPkgs.stdenv.hostPlatform.system};
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
      ];
      text = # bash
        ''
          skopeo login -u="$REPO_USERNAME" -p="$REPO_TOKEN" ${server}
          regctl registry login -u="$REPO_USERNAME" -p="$REPO_TOKEN" ${server}
          ${copyToRegistry "aarch64-linux"}
          ${copyToRegistry "x86_64-linux"}
          regctl index create ${repo}/lix:${pkgs.lixPackageSets.lix_2_93.lix.version} \
            --ref ${imageRef "aarch64-linux"} \
            --ref ${imageRef "x86_64-linux"}
          regctl index create ${repo}/lix:latest \
            --ref ${imageRef "aarch64-linux"} \
            --ref ${imageRef "x86_64-linux"}
        '';
    };
}

# SPDX-License-Identifier: MIT

let
  inputs =
    (
      let
        lock = builtins.fromJSON (builtins.readFile ./flake.lock);
        flake-compatish = import (builtins.fetchTree lock.nodes.flake-compatish.locked);
      in
      flake-compatish {
        source = ./.;
        overrides = {
          self = ./.;
        };
      }
    ).inputs;
in
{
  system ? builtins.currentSystem,
}:
rec {
  pkgs = import inputs.nixpkgs {
    inherit system;
    overlays = [ (import ./pkgs) ];
  };
  inherit inputs;
  lib = pkgs.lib;

  easykubenix = import inputs.easykubenix;

  kubenixApply = kubenixInstance { };
  kubenixCI1 = kubenixInstance {
    module.imports = [
      ./kubenix/ci
    ];
  };
  # kubenixCI2 is used by tests/nixos/integration.nix for the containerd nixos test.
  # Disables aarch64-linux to avoid needing cross-compilation support.
  kubenixCI2 = kubenixInstance {
    module.imports = [
      ./kubenix/ci
      {
        nixkube.cache.enable = false;
        nixkube.builders.enable = false;
        # push = true retains Nix string context on DaemonSet store paths so
        # they become part of the manifest's closure.  The NixOS test VM then
        # has every path in /nix/store, where nix-serve makes them available
        # as a substituter for nixkube's separate /var/lib/nix-csi store.
        nixkube.push = true;
        nixkube.systems = {
          x86_64-linux = true;
          aarch64-linux = false;
        };
        # 10.113.37.1 is the PTP CNI gateway — the host-side veth IP reachable
        # from all pods.  nix-serve runs there during the NixOS test.
        nixkube.node.nixConfig.settings.substituters = [
          "http://10.113.37.1:5000?trusted=1"
        ];
      }
    ];
  };
  kubenixLocal = kubenixInstance {
    module.imports = [
      ./kubenix/ci
      (
        { config, ... }:
        {
          kluctl.preDeployScript = # bash
            ''
              expected_context="kind"
              current_context=$(kubectl config current-context)

              if [[ "$current_context" != *"$expected_context" ]]; then
                  echo "Warning: Current context is $current_context, not *$expected_context"* >&2
                  read -rp "Continue anyway? [y/N] " confirm
                  if [[ ! "$confirm" =~ ^[Yy]$ ]]; then
                      echo "Aborted." >&2
                      exit 1
                  fi
              fi
              cachix push nix-csi ${config.internal.manifestJSONFile}
            '';
          nixkube.cache.enable = true;
          nixkube.builders.enable = true;
          nixkube.push = true;
        }
      )
    ];
  };
  kubenixPush = kubenixInstance {
    module.config = {
      nixkube.push = true;
      nixkube.systems = {
        ${builtins.currentSystem} = true;
      };
    };
  };
  kubenixPushBoth = kubenixInstance {
    module.config = {
      nixkube.push = true;
      nixkube.systems = {
        x86_64-linux = true;
        aarch64-linux = true;
      };
    };
  };
  kubenixInstance =
    {
      module ? { },
    }:
    easykubenix {
      inherit pkgs;
      modules = [
        module
        ./kubenix
        {
          _module.args.inputs = inputs;
        }
        {
          config = {
            # Disabled by default so you can include the module in an easykubenix project
            nixkube.enable = true;
            # Allow easily adding your pubkeys to the cache
            nixkube.authorizedKeys = lib.pipe (lib.filesystem.listFilesRecursive ./keys) [
              (lib.filter (name: lib.hasSuffix ".pub" name))
              (lib.map (name: builtins.readFile name))
              (lib.map (key: lib.trim key))
            ];
          };
        }
      ];
    };

  push =
    pkgs.writeScriptBin "push" # bash
      ''
        #! ${pkgs.runtimeShell}
        export PATH=${lib.makeBinPath [ pkgs.cachix ]}:$PATH
        # ${lib.concatStrings (lib.attrValues inputs)}
        nix-store -qR --include-outputs $(nix-store -qd ${kubenixPush.deploymentScript}) | grep -v '\.drv$' | cachix push nix-csi
      '';

  # Push environments for both x86_64-linux and aarch64-linux to cachix.
  # Requires builders that support both architectures (e.g. nixbuild.net or ssh builders).
  push-env =
    pkgs.writeScriptBin "push-env" # bash
      ''
        #! ${pkgs.runtimeShell}
        export PATH=${lib.makeBinPath [ pkgs.cachix ]}:$PATH
        # ${lib.concatStrings (lib.attrValues inputs)}
        nix-store -qR --include-outputs $(nix-store -qd ${kubenixPushBoth.deploymentScript}) | grep -v '\.drv$' | cachix push nix-csi
      '';

  uploadScratch =
    let
      scratchVersion = "1.0.1";
      scratchUrl = system: "ghcr.io/lillecarl/nix-csi/scratch:${scratchVersion}-${system}";
      scratchManifest = "ghcr.io/lillecarl/nix-csi/scratch:${scratchVersion}";
    in
    pkgs.writeScriptBin "uploadScratch" # bash
      ''
        #! ${pkgs.runtimeShell}
        set -euo pipefail
        set -x
        export PATH=${lib.makeBinPath [ pkgs.buildah ]}:$PATH
        # Build and publish scratch image(s)
        buildah login -u="$REPO_USERNAME" -p="$REPO_TOKEN" ghcr.io
        container=$(buildah from --platform linux/amd64 scratch)
        buildah config --env "PATH=/nix/var/result/bin" $container
        buildah commit $container ${scratchUrl "x86_64-linux"}
        buildah push ${scratchUrl "x86_64-linux"}
        container=$(buildah from --platform linux/arm64 scratch)
        buildah config --env "PATH=/nix/var/result/bin" $container
        buildah commit $container ${scratchUrl "aarch64-linux"}
        buildah push ${scratchUrl "aarch64-linux"}
        buildah manifest rm ${scratchManifest} &>/dev/null || true
        buildah manifest create ${scratchManifest}
        buildah manifest add ${scratchManifest} ${scratchUrl "x86_64-linux"}
        buildah manifest add ${scratchManifest} ${scratchUrl "aarch64-linux"}
        buildah manifest push ${scratchManifest}
      '';
  genModDoc =
    let
      optionsDocs = pkgs.nixosOptionsDoc {
        inherit (kubenixCI1.eval) options;
        warningsAreErrors = false;
        transformOptions =
          opt:
          opt
          // {
            # Remove internal options, modify declarations, etc.
            visible =
              opt.visible or true && (lib.hasPrefix "nix-csi" opt.name || lib.hasPrefix "nixkube" opt.name);
            declarations = map (
              decl:
              let
                prefix = builtins.toString ./.;
              in
              if lib.hasPrefix prefix (toString decl) then lib.removePrefix prefix (toString decl) else decl
            ) opt.declarations;
          };
      };
    in
    pkgs.writeScriptBin "genModDoc" # bash
      ''
        #! ${pkgs.runtimeShell}
        cp --no-preserve=mode ${optionsDocs.optionsCommonMark} $GIT_ROOT/doc/options.md
      '';

  # NixOS integration tests — spin up real kubeadm clusters in VMs
  nixosTests = {
    containerd = import ./tests/nixos/integration.nix {
      inherit pkgs lib;
      manifests = kubenixCI2.manifestYAMLFile;
    };
  };

  treefmt = inputs.treefmt-nix.lib.mkWrapper pkgs {
    projectRootFile = "flake.nix";
    programs.fish_indent.enable = true;
    programs.isort.enable = true;
    programs.nixfmt.enable = true;
    programs.ruff-check.enable = true;
    programs.ruff-format.enable = true;
    programs.shellcheck.enable = true;
    programs.typos.enable = true;
    programs.yamlfmt.enable = true;
  };

  lixImage = pkgs.callPackage ./liximage.nix { };
  scratchImage = pkgs.callPackage ./scratchimage.nix { };
}

/*
  cert-manager for a vivarium cluster: its three binaries from source,
  images that hold symlinks into /nix/store, and upstream's release
  manifest with the store mounted into each Deployment.

  nixpkgs packages only `cmctl`. Each binary is a Go module of its own
  under `cmd/`, which replaces the root module with `../../`.
*/
{
  lib,
  buildGoModule,
  fetchFromGitHub,
  fetchurl,
  runCommand,
  dockerTools,
  yq-go,
}:
let
  version = "1.21.3";

  src = fetchFromGitHub {
    owner = "cert-manager";
    repo = "cert-manager";
    tag = "v${version}";
    hash = "sha256-qqUbhmh8kwEOQ3AWJ9pmEv52Jh/FwLjn0bCI02rv5fE=";
  };

  binary =
    name: vendorHash:
    buildGoModule {
      pname = "cert-manager-${name}";
      inherit version src vendorHash;
      modRoot = "cmd/${name}";
      subPackages = [ "." ];
      env.CGO_ENABLED = 0;
      ldflags = [
        "-s"
        "-w"
        "-X github.com/cert-manager/cert-manager/pkg/util.AppVersion=v${version}"
      ];
      # The module is `<name>-binary`; the image runs it by its own name.
      postInstall = ''
        mv $out/bin/${name}-binary $out/bin/cert-manager-${name}
      '';
      meta = {
        description = "cert-manager's ${name}";
        homepage = "https://cert-manager.io";
        license = lib.licenses.asl20;
        mainProgram = "cert-manager-${name}";
      };
    };

  binaries = {
    controller = binary "controller" "sha256-FZYEqnFHGajdkQHzNWUmqAV+eZq8lBkzTMA3cedwXOc=";
    webhook = binary "webhook" "sha256-Y84d9+K7t91fBEY+oIa4eRHVftbmIOyXV4VX/3+L4SY=";
    cainjector = binary "cainjector" "sha256-1qm8IPhrJop0Lw78pohswRX6N6BClbFrOkvVpm4DdgM=";
  };

  # Upstream's names and tag: the manifest asks for these, with
  # `imagePullPolicy: IfNotPresent`, so a node that imported them pulls
  # nothing.
  image =
    name: package:
    dockerTools.buildLayeredImage {
      name = "quay.io/jetstack/cert-manager-${name}";
      tag = "v${version}";
      includeStorePaths = false;
      extraCommands = ''
        mkdir -p usr/local/bin tmp
        ln -sfn ${lib.getExe package} usr/local/bin/cert-manager-${name}
      '';
      config = {
        Entrypoint = [ "/usr/local/bin/cert-manager-${name}" ];
        Env = [ "PATH=/usr/local/bin" ];
        WorkingDir = "/";
        # Upstream's images run as this user; the manifest's
        # `runAsNonRoot` refuses an image without one.
        User = "1000";
      };
    };

  release = fetchurl {
    url = "https://github.com/cert-manager/cert-manager/releases/download/v${version}/cert-manager.yaml";
    hash = "sha256-wEpRKVrdorGndRRnUflz54cZB3jH81GAhh2WeIduXHQ=";
  };
in
{
  inherit version binaries;

  tarball = dockerTools.mergeImages (lib.mapAttrsToList image binaries);

  /*
    `release`, with each Deployment's pod given the store, read-only, as
    every pod of a vivarium cluster declares it: modules/k8s.nix says why
    the node does not mount it for them.
  */
  manifest = runCommand "cert-manager-${version}.yaml" { nativeBuildInputs = [ yq-go ]; } ''
    yq eval '
      (select(.kind == "Deployment") | .spec.template.spec.volumes) += [{"name": "nix-store", "hostPath": {"path": "/nix/store", "type": "Directory"}}] |
      (select(.kind == "Deployment") | .spec.template.spec.containers[].volumeMounts) += [{"name": "nix-store", "mountPath": "/nix/store", "readOnly": true}]
    ' ${release} > $out
  '';
}

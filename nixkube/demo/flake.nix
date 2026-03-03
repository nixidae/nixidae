# nix copy .#manifest
# kubectl apply -f $(nix build .#manifest)
{
  inputs.nixpkgs.url = "github:nixos/nixpkgs/nixos-unstable";
  inputs.easykubenix.url = "github:lillecarl/easykubenix";
  outputs = inputs: {
    packages.x86_64-linux.manifest =
      (inputs.easykubenix.lib.easykubenix {
        pkgs = import inputs.nixpkgs { system = "x86_64-linux"; };
        modules = [
          (
            { pkgs, lib, ... }:
            {
              kubernetes.resources.none.Pod.hello.spec = {
                nodeSelector."kubernetes.io/arch" = pkgs.go.GOARCH;
                containers = lib.mkNamedList {
                  hello = {
                    image = "ghcr.io/lillecarl/nix-csi/scratch:1.0.1"; # 1.0.1 sets PATH to /nix/var/result/bin
                    command = [ "echoserver" ]; # or (lib.getExe pkgs.hello)
                    volumeMounts = lib.mkNamedList {
                      nix = {
                        mountPath = "/nix";
                        subPath = "nix";
                      };
                    };
                  };
                };
                volumes = lib.mkNamedList {
                  nix.csi = {
                    driver = "nix.csi.store"; # soon nixkube
                    volumeAttributes.x86_64-linux = pkgs.hello; # not needed if you stick store path in command
                  };
                };
              };
            }
          )
        ];
      }).manifestYAMLFile;
  };
}

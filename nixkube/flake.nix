{
  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";
    flake-compatish = {
      url = "github:lillecarl/flake-compatish";
      flake = false;
    };
    easykubenix = {
      url = "github:lillecarl/easykubenix";
      flake = false;
    };
    dinix = {
      url = "github:lillecarl/dinix";
      flake = false;
    };
  };
  outputs =
    inputs:
    let
      inherit (inputs.nixpkgs) lib;
      gen = func: lib.genAttrs [ "aarch64-linux" "x86_64-linux" ] func;
    in
    {
      packages = gen (
        system:
        let
          pkgs = import inputs.nixpkgs { inherit system; };
        in
        {
          inherit (pkgs) hello;
          init-secrets = pkgs.writeShellApplication {
            name = "push";
            runtimeInputs = [ pkgs.coreutils pkgs.openssh  ];
            text = # bash
            ''
              set -euo pipefail
              mkdir -p /tmp/{ssh-key,nix-key}
              if ! kubectl get secret ssh-key; then
                # Create ssh secret
                ssh-keygen -t ed25519 -C "" -N "" -f /tmp/ssh-key/id_ed25519
                kubectl create secret generic ssh-key \
                  --from-file=/tmp/ssh-key/id_ed25519.pub \
                  --from-file=/tmp/ssh-key/id_ed25519

                # Create pubkey configmap
                cp /tmp/ssh-key/id_ed25519.pub /tmp/ssh-key/authorized_keys
                echo "* $(cat /tmp/ssh-key/id_ed25519.pub)" > /tmp/ssh-key/ssh_known_hosts
                kubectl create configmap ssh-dynauth \
                  --from-file=/tmp/ssh-key/authorized_keys \
                  --from-file=/tmp/ssh-key/ssh_known_hosts
              fi
              if ! kubectl get secret nix-key; then
                # create nix binary cache key
                nix-store --generate-binary-cache-key \
                  nix-cache-1 \
                  /tmp/nix-key/nix_ed25519 \
                  /tmp/nix-key/nix_ed25519.pub
                kubectl create secret generic ssh-key \
                  --from-file=/tmp/nix-key/nix_ed25519.pub \
                  --from-file=/tmp/nix-key/nix_ed25519
              fi
            '';
          };
        }
      );
    };
}

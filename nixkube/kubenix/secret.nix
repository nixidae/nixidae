# SPDX-License-Identifier: MIT

{
  config,
  lib,
  mkNCSI,
  curPkgs,
  ...
}:
let
  cfg = config.nix-csi;
in
{
  config = lib.mkIf cfg.enable {
    kubernetes.resources.${cfg.namespace} = {
      ConfigMap.ssh-config = mkNCSI {
        data = {
          # Keys that can connect to us
          "authorized_keys" = lib.concatLines cfg.authorizedKeys;
          # Keys that we can connect to
          "ssh_known_hosts" = lib.concatLines (lib.mapAttrsToList (n: v: "${n} ${v}") cfg.knownHosts);
          # Client configuration
          "ssh_config" = ''
            GlobalKnownHostsFile /etc/ssh/ssh_known_hosts /etc/ssh-dynauth/ssh_known_hosts

            Host nix-cache
                User nix
                IdentityFile /etc/ssh-key/id_ed25519
                IdentitiesOnly yes
                StrictHostKeyChecking yes

            Host nix-builder
                User nix
                IdentityFile /etc/ssh-key/id_ed25519
                IdentitiesOnly yes
                StrictHostKeyChecking yes

            Host *
                User nix
                IdentityFile /etc/ssh-key/id_ed25519
                IdentitiesOnly yes
                StrictHostKeyChecking yes
          '';
          # Server configuration
          "sshd_config" = ''
            Port 22
            AddressFamily Any
            HostKey /etc/ssh-key/id_ed25519
            SyslogFacility DAEMON
            SetEnv PATH=/nix/var/result/bin
            PermitRootLogin no
            PubkeyAuthentication yes
            PasswordAuthentication no
            KbdInteractiveAuthentication no
            UsePAM no
            AuthorizedKeysFile /dev/null
            StrictModes no
            Subsystem sftp internal-sftp

            Match User nix
                AuthorizedKeysFile /etc/ssh/authorized_keys /etc/ssh-dynauth/authorized_keys
          '';
        };
      };
      ConfigMap.init-scripts = {
        data.init-secrets = # bash
          ''
            set -x
            mkdir -p /tmp/{ssh-key,nix-key,ssh-auth}
            if ! kubectl get secret ssh-key &>/dev/null || ! kubectl get configmap ssh-dynauth &>/dev/null; then
              # Create ssh secret
              ssh-keygen -t ed25519 -C "" -N "" -f /tmp/ssh-key/id_ed25519
              kubectl delete --ignore-not-found secret ssh-key
              kubectl create secret generic ssh-key --from-file=/tmp/ssh-key

              # Create pubkey configmap
              cp /tmp/ssh-key/id_ed25519.pub /tmp/ssh-auth/authorized_keys
              echo "* $(cat /tmp/ssh-key/id_ed25519.pub)" > /tmp/ssh-auth/ssh_known_hosts
              kubectl delete --ignore-not-found configmap ssh-dynauth
              kubectl create configmap ssh-dynauth --from-file=/tmp/ssh-auth
            fi

            if ! kubectl get secret nix-key &>/dev/null; then
              # create nix binary cache key
              nix-store --generate-binary-cache-key \
                nix-cache-1 \
                /tmp/nix-key/nix_ed25519 \
                /tmp/nix-key/nix_ed25519.pub
              kubectl delete --ignore-not-found secret nix-key
              kubectl create secret generic nix-key --from-file=/tmp/nix-key
            fi
          '';
      };
      Job.init = mkNCSI {
        metadata.annotations = {
          "kluctl.io/hook" = "pre-deploy";
          "kluctl.io/hook-delete-policy" = "hook-succeeded"; # seems flaky
          "kluctl.io/hook-wait" = "false"; # true fails CI since other resources aren't deployed yet
        };
        spec = {
          # ttlSecondsAfterFinished = 0; # remove job when it's done
          template = {
            metadata.labels = {
              "app.kubernetes.io/component" = "init";
            };
            spec = {
              restartPolicy = "OnFailure";
              serviceAccountName = "nix-csi";
              containers = lib.mkNamedList {
                init = {
                  # Use normal lix so we don't have to build lruLix locally
                  image = "ghcr.io/lillecarl/nix-csi/lix:${curPkgs.stdLix.version}";
                  imagePullPolicy = "Always";
                  command = [ "init-secrets" ];
                  volumeMounts = lib.mkNamedList {
                    nix-config.mountPath = "/etc/nix";
                    init-scripts.mountPath = "/opt/bin";
                  };
                };
              };
              volumes = lib.mkNamedList {
                nix-config.configMap.name = "nix-builder";
                init-scripts.configMap.name = "init-scripts";
              };
            };
          };
        };
      };
    };
  };
}

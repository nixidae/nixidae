{
  pkgs,
  config,
  lib,
  mkNCSI,
  ...
}:
let
  cfg = config.nix-csi;
in
{
  config =
    let
      sshKeys =
        pkgs.runCommand "sshKeys"
          {
            nativeBuildInputs = [
              pkgs.openssh
            ];
          }
          # bash
          ''
            # ${toString builtins.currentTime}
            mkdir $out
            ssh-keygen -t ed25519 -C "" -N "" -f $out/id_ed25519
          '';
      nixKeys =
        pkgs.runCommand "nixKeys"
          {
            nativeBuildInputs = [
              pkgs.nix
            ];
          } # bash
          ''
            # ${toString builtins.currentTime}
            mkdir $out
            NIX_STATE_DIR=$TMP nix-store --generate-binary-cache-key --readonly-mode nix-cache-1 $out/pub $out/priv
          '';

      ignoreAnnotations = {
        "kluctl.io/ignore-diff" = "true";
        "kluctl.io/skip-delete" = "true";
      };
    in
    lib.mkIf cfg.enable {
      kluctl.postDeployScript = # bash
        ''
          echo run the following if you deployed secrets
          echo nix store delete ${sshKeys} ${nixKeys}
        '';
      kubernetes.resources.${cfg.namespace} = {
        Secret.ssh-key = lib.mkIf cfg.deploySecrets (mkNCSI {
          metadata.annotations = ignoreAnnotations;
          stringData = {
            "id_ed25519.pub" = builtins.readFile "${sshKeys}/id_ed25519.pub";
            "id_ed25519" = builtins.readFile "${sshKeys}/id_ed25519";
          };
        });
        Secret.nix-key = lib.mkIf cfg.deploySecrets (mkNCSI {
          metadata.annotations = ignoreAnnotations;
          stringData = {
            "pub" = builtins.readFile "${nixKeys}/pub";
            "priv" = builtins.readFile "${nixKeys}/priv";
          };
        });
        ConfigMap.ssh-dynauth = lib.mkIf cfg.deploySecrets (mkNCSI {
          metadata.annotations = ignoreAnnotations;
          data = {
            "ssh_known_hosts" = "* ${builtins.readFile "${sshKeys}/id_ed25519.pub"}";
            "authorized_keys" = builtins.readFile "${sshKeys}/id_ed25519.pub";
          };
        });
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
      };
    };
}

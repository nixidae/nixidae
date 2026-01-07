{
  config,
  lib,
  ...
}:
let
  cfg = config.nix-csi;
in
{
  config = lib.mkIf cfg.enable {
    kubernetes.resources.${cfg.namespace} = {
      ConfigMap.ssh-config.data = {
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
}

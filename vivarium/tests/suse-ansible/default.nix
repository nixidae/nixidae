/*
  Ansible from a NixOS controller configures openSUSE over a segment.

  `ctl` holds the key; the SUSE seed authorises it for `ansible`, with
  sudo. The playbook writes what Ansible's facts say the target is,
  and the agent reads it back on SUSE. An unreachable address is the
  negative control.
*/
{
  pkgs,
  vivarium,
  mkTest,
  images,
  ...
}:
let
  key = pkgs.runCommand "ansible-key" { nativeBuildInputs = [ pkgs.openssh ]; } ''
    mkdir $out
    ssh-keygen -q -t ed25519 -N "" -C ansible -f $out/id_ed25519
  '';
  playbook = pkgs.writeText "site.yml" (
    builtins.toJSON [
      {
        hosts = "suse";
        become = true;
        tasks = [
          {
            name = "say what the target is";
            "ansible.builtin.copy" = {
              dest = "/etc/vivarium-ansible";
              content = "{{ ansible_distribution }} {{ ansible_distribution_version }}\n";
              mode = "0644";
            };
          }
          {
            name = "a user";
            "ansible.builtin.user".name = "deploy";
          }
        ];
      }
    ]
  );
in
mkTest {
  name = "suse-ansible";
  settings = {
    playbook = "${playbook}";
    key = "${key}/id_ed25519";
  };
  nodes.suse = {
    imports = [ vivarium.kinds.image ];
    vivarium.image = images.opensuse-leap-16_0 // {
      userData.users = [
        "default"
        {
          name = "ansible";
          sudo = "ALL=(ALL) NOPASSWD:ALL";
          ssh_authorized_keys = [ (builtins.readFile "${key}/id_ed25519.pub") ];
        }
      ];
    };
    vivarium.memory = "1024M";
    vivarium.interfaces.lab = {
      segment = "lab";
      addresses = [ "10.9.0.2/24" ];
    };
  };
  nodes.ctl =
    { settings, ... }:
    {
      vivarium.lateClosure = [
        settings.playbook
        settings.key
      ];
      vivarium.interfaces.lab = {
        segment = "lab";
        addresses = [ "10.9.0.1/24" ];
      };
      environment.systemPackages = [
        pkgs.ansible
        pkgs.openssh
      ];
    };
  phases.ansible = {
    script = ../phases/suse-ansible.py;
    after = [ "boot" ];
  };
}

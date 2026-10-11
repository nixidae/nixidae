#!nix
# salt-ssh <host> state.apply bootstrap pillar='{bootstrap: {salt: <env>, master: <address>}}'
#
# `salt` is a store path with Salt and havsorm that the host already has.
{ grains, pillar, ... }:
let
  inherit (pillar.bootstrap) salt master;
in
[
  {
    minion-config."file.managed" = [
      { name = "/etc/salt/minion"; }
      { makedirs = true; }
      { contents = builtins.toJSON { inherit master; id = grains.id; }; }
    ];
  }
  {
    minion-unit."file.managed" = [
      { name = "/etc/systemd/system/salt-minion.service"; }
      {
        contents = ''
          [Unit]
          Description=Salt Minion, from ${salt}
          After=network-online.target

          [Service]
          ExecStart=${salt}/bin/salt-minion
          KillMode=process
          Restart=on-failure

          [Install]
          WantedBy=multi-user.target
        '';
      }
    ];
  }
  {
    salt-minion."service.running" = [
      { enable = true; }
      {
        watch = [
          { file = "minion-config"; }
          { file = "minion-unit"; }
        ];
      }
    ];
  }
]

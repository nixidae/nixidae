#!nix
# haproxy in front of every minion with the `web` role, at the address
# each one publishes to the mine:
#
#   salt lb state.apply lb pillar='{lb: {unit: /nix/store/...-havsorm-lb.service}}'
{ salt, pillar, ... }:
let
  inherit (builtins) attrNames concatStringsSep head;
  webs = salt "mine.get" {
    tgt = "roles:web";
    tgt_type = "grain";
    fun = "lab_ip";
  };
  server = id: "  server ${id} ${head webs.${id}}:8080 check\n";
  config = ''
    defaults
      mode http
      timeout connect 2s
      timeout client 5s
      timeout server 5s
    frontend web
      bind *:80
      default_backend webs
    backend webs
      balance roundrobin
      default-server inter 500ms fall 2 rise 2
  '' + concatStringsSep "" (map server (attrNames webs));
in
[
  {
    lb-config."file.managed" = [
      { name = "/etc/havsorm-lb.cfg"; }
      { contents = config; }
    ];
  }
  {
    lb-unit."havsorm.unit" = [
      { name = "havsorm-lb.service"; }
      { path = pillar.lb.unit; }
      { unit_dir = "/run/systemd/system"; }
    ];
  }
  {
    havsorm-lb."service.running" = [
      {
        watch = [
          { havsorm = "lb-unit"; }
          { file = "lb-config"; }
        ];
      }
    ];
  }
]

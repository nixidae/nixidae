#!nix
# A web server from the store, on any distribution:
#
#   salt -G roles:web state.apply web pillar='{web: {unit: /nix/store/...-havsorm-web.service}}'
#
# NixOS keeps /etc/systemd/system in the store, read-only, so the unit
# goes to the runtime directory there and is not enabled.
{ grains, pillar, ... }:
let
  nixos = grains.os == "NixOS";
in
[
  {
    web-page."file.managed" = [
      { name = "/var/lib/havsorm-web/index.html"; }
      { makedirs = true; }
      { contents = "${grains.id}\n"; }
    ];
  }
  {
    web-unit."havsorm.unit" = [
      { name = "havsorm-web.service"; }
      { path = pillar.web.unit; }
      { unit_dir = if nixos then "/run/systemd/system" else "/etc/systemd/system"; }
    ];
  }
  {
    havsorm-web."service.running" = [
      { enable = !nixos; }
      {
        watch = [
          { havsorm = "web-unit"; }
          { file = "web-page"; }
        ];
      }
    ];
  }
]

#!nix
{ grains, ... }:
{
  fleet-file."file.managed" = [
    { name = "/etc/havsorm-fleet"; }
    { contents = "${grains.os} ${grains.id}\n"; }
  ];
}

#!nix
# `salt/beacon/<id>/service/<name>`: start a service the service beacon
# saw stopped.
{ tag, data, ... }:
let
  inherit (builtins) elemAt filter isString split;
  id = elemAt (filter isString (split "/" tag)) 2;
  event = data.data or data;
  name = event.service_name;
in
if event.${name}.running then
  { }
else
  {
    "heal-${id}-${name}"."local.service.start" = [
      { tgt = id; }
      { args = [ { name = name; } ]; }
    ];
  }

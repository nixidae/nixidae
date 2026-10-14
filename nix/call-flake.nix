# Read a flake we do not own, with our sources in place of its own lock file.
#
# Takes the *name*, not a directory. flake-compatish gets a path for a
# directory in this tree, and a locked reference for a pin. A fetched
# directory would not do: it is a string carrying store-path context, and
# flake-compatish would hand that to parseFlakeRef, which a pure evaluation
# refuses.
#
# The overrides are the whole set. flake-compatish reads the flake's own
# flake.nix for the names it declares and ignores the rest. A name it
# declares that we do not carry comes from its own lock.
{
  sources ? import ./wire.nix { },
}:
let
  lock = builtins.fromJSON (builtins.readFile ../flake.lock);
  ref =
    name: value:
    if builtins.isPath value then
      value
    else
      let
        locked = lock.nodes.${lock.nodes.${lock.root}.inputs.${name}}.locked;
      in
      "github:${locked.owner}/${locked.repo}/${locked.rev}?narHash=${
        builtins.replaceStrings [ "=" ] [ "%3D" ] locked.narHash
      }";
  refs = builtins.mapAttrs ref sources;
in
name:
(import sources.flake-compatish {
  source = refs.${name};
  overrides = refs;
  warnOverrides = false;
}).outputs

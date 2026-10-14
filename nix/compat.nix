# `flake.lock` for `default.nix`, so the flake and a build from the file read
# one lock. Every input is `flake = false`, so the lock is flat and one
# `fetchTree` per node reads it, purely.
let
  lock = builtins.fromJSON (builtins.readFile ../flake.lock);
in
builtins.mapAttrs (
  _: node: builtins.fetchTree lock.nodes.${node}.locked
) lock.nodes.${lock.root}.inputs

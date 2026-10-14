# Where every dependency of pydd lives: nixidae
# (https://github.com/nixidae/nixidae), the repository this one is exported
# from. Work on pydd happens there.
#
# Inside nixidae this directory is `<nixidae>/pydd`, and nixidae's own
# nix/wire.nix answers. On its own, `flake.lock` names the nixidae commit to
# build against, and this tree takes the place of its copy there.
let
  root = toString ../.;
  # `../..` from a bare store path leaves the store root, and Nix refuses to
  # evaluate it. `builtins.tryEval` does not catch that.
  escapesStore = builtins.match "/nix/store/[^/]+" root != null;
  inNixidae = !escapesStore && builtins.pathExists ../../nix/wire.nix;

  lock = builtins.fromJSON (builtins.readFile ../flake.lock);
  nixidae = builtins.fetchTree lock.nodes.${lock.nodes.${lock.root}.inputs.nixidae}.locked;

  wire = if inNixidae then ../../nix/wire.nix else "${nixidae}/nix/wire.nix";
in
import wire { overrides.pydd = ../.; }

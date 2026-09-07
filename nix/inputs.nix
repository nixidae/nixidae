# Every input this collection uses, named once.
#
# The umbrella owns them. Each project still declares its own inputs in its
# own flake.nix, and that declaration is what a build of it on its own falls
# back to. Inside this checkout the umbrella wins: default.nix hands this set
# to flake-compatish as overrides, so all four projects see one nixpkgs and
# one copy of every dependency they share.
#
# A name nobody declares is ignored, so this set does not have to say which
# project wants what. flake-compatish reads each project's own flake.nix for
# that, and takes from here only the names it finds there.
#
# Two files make this set, and each has one writer:
#
#   nix/sources.nix   where a source comes from. A human writes it.
#   nix/sources.lock  which revision. A tool writes it.
#
# nix/resolve.nix joins them. A name with a working copy in this checkout
# resolves to that directory; every other name resolves to the revision in
# the lock. Read those three files, not this one.
import ./resolve.nix {
  spec = import ./sources.nix;
  lock = builtins.fromJSON (builtins.readFile ./sources.lock);
}

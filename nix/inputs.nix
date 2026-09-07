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
# Two kinds of value, and they behave differently on purpose.
#
#   A path is read where it lies. Nothing is copied to the store, so an edit
#   in one project reaches the next build of another with no commit, no push
#   and no pin to bump. That is the whole reason these five repositories sit
#   in one checkout.
#
#   A string is a flake reference. It carries no revision, so it is fetched
#   impurely and gets whatever the branch holds today. There is no lock file
#   here to go stale and nothing to maintain, and the cost is that a build is
#   best-effort rather than reproducible. Write a revision into the string
#   when one of them starts moving under us.
{
  # The four projects, and each other. Relative to this file, so they name
  # the working copies one directory up.
  nanopynix = ../nanopynix;
  pynixd = ../pynixd;
  easykubenix = ../easykubenix;
  nixkube = ../nixkube;

  # From NIX_PATH, which is this machine's own nixpkgs. It is already built
  # and already in the cache, so it costs nothing, and it is the same one
  # `nix-shell` and `nix build --file .` reach for everywhere else here.
  nixpkgs = <nixpkgs>;

  flake-compatish = "github:lillecarl/flake-compatish";
  pyproject-nix = "github:pyproject-nix/pyproject.nix";
  tree-sitter-nix-numtide = "github:numtide/tree-sitter-nix";
  adios = "github:adisbladis/adios";
  treefmt-nix = "github:numtide/treefmt-nix";
  dinix = "github:lillecarl/dinix";
}

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

  # Pinned, and the only one here that is.
  #
  # Every evaluation goes through this one, always: nix/wire.nix fetches it
  # before it can read anything else. An unpinned reference is impure, so a
  # pure evaluation that reaches wire.nix fails on this line and on nothing
  # else -- measured:
  #
  #   error: in pure evaluation mode, 'fetchTree' doesn't fetch unlocked
  #          input 'github:lillecarl/flake-compatish'
  #
  # A revision costs one edit when the tool changes, and the tool is the
  # part of this that changes least. It also stops the bootstrap drifting
  # under every project at once, which is worth more here than currency.
  #
  # This does not make a pure evaluation work on its own. `nixpkgs` below is
  # a NIX_PATH lookup, which is impure as well, and the five references
  # under it are unpinned. See the README.
  flake-compatish = "github:lillecarl/flake-compatish/ceadbe462830c595f3b0c0ef212d039cba5a48ae";
  pyproject-nix = "github:pyproject-nix/pyproject.nix";
  tree-sitter-nix-numtide = "github:numtide/tree-sitter-nix";
  adios = "github:adisbladis/adios";
  treefmt-nix = "github:numtide/treefmt-nix";
  dinix = "github:lillecarl/dinix";
}

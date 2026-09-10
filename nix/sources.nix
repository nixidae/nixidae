# Where every source comes from. One entry per name.
#
# This file is the specification and a human writes it. `nix/sources.lock`
# holds the revisions and a tool writes it. Nothing reads this file to build:
# `nix/inputs.nix` joins the two.
#
# Fields:
#
#   url     The repository, over https. https is what a clone with no key can
#           reach, so it is what a CI runner and a fresh checkout get. A user
#           who pushes over SSH sets it once in their own gitconfig:
#
#             git config --global url."git@github.com:".insteadOf \
#               "https://github.com/"
#
#           git rewrites every https URL here when it clones, so the
#           transport is the user's choice and not ours.
#
#   branch  What `umbrella update` follows when it writes a new revision.
#
#   path    Optional. Where a working copy of this source goes. It has to be
#           the directory beside this repository named after the source:
#           `umbrella fetch` clones there, and `umbrella update` refuses when
#           the two disagree.
#
#           When the directory holds anything, `nix/resolve.nix` reads that
#           checkout at the revision the lock names, so a build here and a
#           build in CI agree. UMBRELLA_DEV names the sources to read as
#           directories instead, which is what makes an uncommitted change in
#           one project reach the next build of another.
#
#           The test is the contents, not the directory. A tarball of this
#           repository has none of them, and neither does a fresh clone: the
#           working copies are ignored, so nothing is committed here about
#           them.
{
  # The seven repositories worked on together here.
  nanopynix = {
    url = "https://github.com/Lillecarl/nanopynix.git";
    branch = "develop";
    path = ../nanopynix;
  };
  pynixd = {
    url = "https://github.com/Lillecarl/pynixd.git";
    branch = "develop";
    path = ../pynixd;
  };
  easykubenix = {
    url = "https://github.com/Lillecarl/easykubenix.git";
    branch = "develop";
    path = ../easykubenix;
  };
  nixkube = {
    url = "https://github.com/Lillecarl/nixkube.git";
    branch = "develop";
    path = ../nixkube;
  };
  umbrella = {
    url = "https://github.com/Lillecarl/umbrella.git";
    branch = "main";
    path = ../umbrella;
  };
  user-mode-nixos = {
    url = "https://github.com/lillecarl/user-mode-nixos.git";
    branch = "develop";
    path = ../user-mode-nixos;
  };

  # Everything the seven ask for.
  #
  # nixpkgs was `<nixpkgs>` here until the lock existed. The lock holds the
  # revision this machine's channel was on, and a locked fetch of that
  # revision gives the same derivations -- measured, `hello.drvPath` is
  # gx2drhxsvkh7xr490rg7dpqyn53iw3z0 either way. So the pin costs no rebuild
  # and buys an evaluation that does not read NIX_PATH.
  nixpkgs = {
    url = "https://github.com/NixOS/nixpkgs.git";
    branch = "nixpkgs-unstable";
  };
  # A working copy like the four projects, because the umbrella workflow is
  # what it has to serve. Every evaluation here goes through it, so a change
  # to it is a change to how all of this resolves, and that is easier to make
  # with the source in the next directory than with a revision to bump.
  flake-compatish = {
    url = "https://github.com/lillecarl/flake-compatish.git";
    branch = "main";
    path = ../flake-compatish;
  };
  pyproject-nix = {
    url = "https://github.com/pyproject-nix/pyproject.nix.git";
    branch = "master";
  };
  tree-sitter-nix-numtide = {
    url = "https://github.com/numtide/tree-sitter-nix.git";
    branch = "master";
  };
  adios = {
    url = "https://github.com/adisbladis/adios.git";
    branch = "master";
  };
  treefmt-nix = {
    url = "https://github.com/numtide/treefmt-nix.git";
    branch = "main";
  };
  dinix = {
    url = "https://github.com/lillecarl/dinix.git";
    branch = "main";
  };
}

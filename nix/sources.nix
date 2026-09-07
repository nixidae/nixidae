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
#           git rewrites every https URL here, and `git submodule sync` keeps
#           the rewrite. So the transport is the user's choice, not ours.
#
#   branch  What `umbrella update` follows when it writes a new revision.
#
#   path    Optional. A working copy in this checkout. When the directory
#           holds a flake.nix, `nix/inputs.nix` reads it where it lies and
#           ignores the lock. That is what makes a change in one project
#           reach the next build of another with no commit and no push.
#
#           The test is the file, not the directory. A clone without
#           --recurse-submodules leaves the directory there and empty.
{
  # The five repositories this checkout holds.
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

  # Everything the five ask for.
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
  flake-compatish = {
    url = "https://github.com/lillecarl/flake-compatish.git";
    branch = "main";
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

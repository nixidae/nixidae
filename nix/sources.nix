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
  # The repositories worked on together here.
  #
  # ghanix takes `lib` and nothing else, so it depends on nothing in this
  # list and nothing here has to be built to use it. It was a directory
  # inside nanopynix until nixkube wanted it too, and nixkube has no
  # nanopynix dependency at all.
  ghanix = {
    url = "https://github.com/Lillecarl/ghanix.git";
    branch = "develop";
    path = ../ghanix;
  };
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
  # Nix C++ bindings generated from a Python DSL. Work in progress, meant to
  # replace nanopynix-bindings.
  huggorm = {
    url = "https://github.com/Lillecarl/huggorm.git";
    branch = "main";
    path = ../huggorm;
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
  # A fork, with a working copy, because we carry patches to it.
  #
  # `ekn` talks to Kubernetes through kr8s and should talk through nothing
  # else. Upstream has no server-side apply, so the one operation every
  # apply goes through had to be built by hand out of `call_api`. See
  # easykubenix issue #29 for that and four more gaps.
  #
  # **Only changes meant for upstreaming.** No codestyle, no local
  # convenience. Every patch has to stand as a PR to kr8s-org/kr8s, or the
  # fork becomes maintenance nobody can hand back. `develop` carries them;
  # `main` stays upstream's.
  kr8s = {
    url = "https://github.com/Lillecarl/kr8s.git";
    branch = "develop";
    path = ../kr8s;
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
  # `buildLayer` takes the store paths of a layer by name, which
  # `dockerTools.streamLayeredImage` cannot: it has `maxLayers` and a
  # popularity heuristic. nixkube's image is 118 paths, and 81 of them are
  # under a megabyte, so the heuristic spends 81 of 125 layers on 5% of the
  # bytes. nixkube builds its node image with this.
  nix2container = {
    url = "https://github.com/nlewo/nix2container.git";
    branch = "master";
  };
  # The OpenTofu registry's own index, which is what lets easykubenix pin a
  # provider nixpkgs does not package, or a version it does not carry.
  #
  # Every provider, every version, every platform: 4242 `providers/<shard>/
  # <owner>/<repo>.json` files, each naming a prebuilt download URL and its
  # shasum. nixpkgs' `terraform-providers` has 169 providers at one version
  # each, which is a curated subset rather than a registry.
  #
  # Fetched only when something reads it. A configuration with no `tf` unit,
  # or one whose providers all come from nixpkgs, never forces this and never
  # downloads the ~424M tree.
  opentofu-registry = {
    url = "https://github.com/opentofu/registry.git";
    branch = "main";
  };
}

# The sources in this tree. nix/wire.nix, lock-subtrees and josh-export all
# read this list.
#
#   repo     the repository josh exports the source to
#   package  a package of its flake, and the attribute path of the same
#            package in nixidae's default.nix; `josh-export --verify`
#            requires the two to be one derivation. null for a source with
#            no flake.
{
  # The organisation's profile and community files. The monorepo's own
  # `.github/` holds its workflows, so the directory is `dotgithub`.
  dotgithub = {
    repo = "nixidae/.github";
    package = null;
  };
  easykubenix = {
    repo = "nixidae/easykubenix";
    package = {
      flake = "default";
      root = "easykubenix.packages.default";
    };
  };
  ghanix = {
    repo = "nixidae/ghanix";
    package = null;
  };
  havsorm = {
    repo = "nixidae/havsorm";
    package = {
      flake = "default";
      root = "havsorm.packages.default";
    };
  };
  huggorm = {
    repo = "nixidae/huggorm";
    package = {
      flake = "default";
      root = "huggorm.packages.default";
    };
  };
  kappy = {
    repo = "nixidae/kappy";
    package = {
      flake = "default";
      root = "kappy.packages.default";
    };
  };
  nanopynix = {
    repo = "nixidae/nanopynix";
    package = {
      flake = "default";
      root = "nanopynix.packages.default";
    };
  };
  nixkube = {
    repo = "nixidae/nixkube";
    package = {
      flake = "default";
      root = "nixkube.docs.default";
    };
  };
  nixvenv = {
    repo = "nixidae/nixvenv";
    package = null;
  };
  pydd = {
    repo = "nixidae/pydd";
    package = {
      flake = "default";
      root = "pydd.packages.default";
    };
  };
  pynixd = {
    repo = "nixidae/pynixd";
    package = {
      flake = "default";
      root = "pynixd.packages.default";
    };
  };
  vivarium = {
    repo = "nixidae/vivarium";
    package = {
      flake = "umlKernel";
      root = "vivarium.packages.umlKernel";
    };
  };
}

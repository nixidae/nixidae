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
      root = "easykubenix.passthru.ekn";
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
      root = "havsorm.havsorm";
    };
  };
  huggorm = {
    repo = "nixidae/huggorm";
    package = {
      flake = "default";
      root = "huggorm.huggorm";
    };
  };
  kappy = {
    repo = "nixidae/kappy";
    package = {
      flake = "default";
      root = "kappy.kappy";
    };
  };
  nanopynix = {
    repo = "nixidae/nanopynix";
    package = {
      flake = "default";
      root = "nanopynix.nanopynix";
    };
  };
  nixkube = {
    repo = "nixidae/nixkube";
    package = {
      flake = "default";
      root = "nixkube.nixkube-docs";
    };
  };
  pydd = {
    repo = "nixidae/pydd";
    package = {
      flake = "default";
      root = "pydd.pydd-drv";
    };
  };
  pynixd = {
    repo = "nixidae/pynixd";
    package = {
      flake = "default";
      root = "pynixd.package";
    };
  };
  vivarium = {
    repo = "nixidae/vivarium";
    package = {
      flake = "umlKernel";
      root = "vivarium.umlKernel";
    };
  };
}

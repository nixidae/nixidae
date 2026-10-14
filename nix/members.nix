# The sources in this tree. nix/wire.nix, lock-subtrees and josh-export all
# read this list.
#
#   repo     the repository josh exports the source to
#   package  a package of its flake, and the attribute path of the same
#            package in nixidae's default.nix; `josh-export --verify`
#            requires the two to be one derivation. null for a source with
#            no flake.
{
  easykubenix = {
    repo = "Lillecarl/easykubenix";
    package = {
      flake = "default";
      root = "easykubenix.passthru.ekn";
    };
  };
  ghanix = {
    repo = "Lillecarl/ghanix";
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
    repo = "Lillecarl/huggorm";
    package = {
      flake = "default";
      root = "huggorm.huggorm";
    };
  };
  nanopynix = {
    repo = "Lillecarl/nanopynix";
    package = {
      flake = "default";
      root = "nanopynix.nanopynix";
    };
  };
  nixkube = {
    repo = "Lillecarl/nixkube";
    package = {
      flake = "default";
      root = "nixkube.nixkube-docs";
    };
  };
  pydd = {
    repo = "Lillecarl/pydd";
    package = {
      flake = "default";
      root = "pydd.pydd-drv";
    };
  };
  pynixd = {
    repo = "Lillecarl/pynixd";
    package = {
      flake = "default";
      root = "pynixd.package";
    };
  };
  vivarium = {
    repo = "Lillecarl/vivarium";
    package = {
      flake = "umlKernel";
      root = "vivarium.umlKernel";
    };
  };
}

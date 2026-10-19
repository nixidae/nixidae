# The pins, and a wrapper. `default.nix` is the whole definition; every
# output here is an attribute of it.
#
# Every input is `flake = false`. The sources import what they want as
# directories, so no input resolves a lock of its own, and `flake.lock` stays
# flat: `nix/compat.nix` reads it for `default.nix` with one `fetchTree` per
# node, so the flake and a build from the file read one lock.
#
# `nix flake update <name>` moves a pin.
{
  inputs = {
    nixpkgs = {
      url = "github:NixOS/nixpkgs/nixpkgs-unstable";
      flake = false;
    };
    # The latest NixOS release. huggorm builds against it too, so a Nix user
    # on a release gets lanes that were tested there (nixidae/nixidae#659).
    nixpkgs-release = {
      url = "github:NixOS/nixpkgs/nixos-26.05";
      flake = false;
    };
    flake-compatish = {
      url = "github:lillecarl/flake-compatish";
      flake = false;
    };
    # Forks for upstream: every patch there has to be one upstream could take.
    # An ignored checkout at ./kr8s or ./containerd takes the place of the pin.
    kr8s = {
      url = "github:Lillecarl/kr8s/develop";
      flake = false;
    };
    containerd = {
      url = "github:Lillecarl/containerd";
      flake = false;
    };
    dinix = {
      url = "github:lillecarl/dinix";
      flake = false;
    };
    pyproject-nix = {
      url = "github:pyproject-nix/pyproject.nix";
      flake = false;
    };
    tree-sitter-nix-numtide = {
      url = "github:numtide/tree-sitter-nix";
      flake = false;
    };
    adios = {
      url = "github:adisbladis/adios";
      flake = false;
    };
    treefmt-nix = {
      url = "github:numtide/treefmt-nix";
      flake = false;
    };
    nix2container = {
      url = "github:nlewo/nix2container";
      flake = false;
    };
    opentofu-registry = {
      url = "github:opentofu/registry";
      flake = false;
    };
  };

  outputs =
    inputs:
    let
      systems = [
        "x86_64-linux"
        "aarch64-linux"
        "aarch64-darwin"
      ];
      perSystem = builtins.listToAttrs (
        map (system: {
          name = system;
          value = import ./default.nix { inherit inputs system; };
        }) systems
      );
    in
    {
      legacyPackages = perSystem;
      devShells = builtins.mapAttrs (_: p: { default = p.shell; }) perSystem;
    };
}

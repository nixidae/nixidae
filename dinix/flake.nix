# The lock and nothing else. Work on dinix happens in nixidae, which this
# repository is exported from. `flake.lock` names the nixidae commit that
# `nix/sources.nix` builds against; lock-subtrees writes it.
#
# dinix is a library, called as `import ./. { pkgs; modules; }`, so the flake
# has no outputs.
{
  inputs.nixidae = {
    url = "github:nixidae/nixidae";
    flake = false;
  };

  outputs = _: { };
}

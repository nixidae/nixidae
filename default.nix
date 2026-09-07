{
  inputs ? import ./nix/inputs.nix,
  system ? builtins.currentSystem,
  # nix/fetch.nix and not `sources` below, because an argument cannot see the
  # body it belongs to.
  pkgs ? import (import ./nix/fetch.nix inputs.nixpkgs) {
    inherit system;
    # nixkube asks for this, and so does nanopynix. One package set for
    # everybody means the umbrella has to ask for it too, or those two would
    # each need a set of their own and the sharing would be gone.
    config.allowUnfree = true;
  },
}:
rec {
  # Every source, as a directory. Each project takes this and imports what it
  # wants; none of them is a flake, so there is nothing else to resolve.
  #
  # nix/wire.nix is the one implementation, and a project's own default.nix
  # calls it too. Passing `inputs` here rather than letting it read
  # nix/inputs.nix itself is what makes an argument to this file reach the
  # projects.
  sources = import ./nix/wire.nix { inherit inputs; };

  # Read a flake we do not own, with these sources in place of its own lock.
  callFlake = import ./nix/call-flake.nix { inherit inputs; };

  # umbrella drives this collection. It keeps a submodule commit that no
  # remote has out of the pointers recorded here, and it makes worktreespaces
  # that share storage instead of cloning every repository again.
  #
  # It is a submodule too, so it can be edited in place like the rest. It is
  # also the tool that checks the submodules out, so a clone made without them
  # has to be able to build it anyway: `sources` gives the revision in
  # nix/sources.lock when the directory is not there.
  umbrella = (import sources.umbrella { inherit pkgs; }).umbrella;

  shell = pkgs.callPackage ./pkgs/shell { inherit umbrella; };

  # -- the four projects ---------------------------------------------------
  #
  # Each one is a repository somebody can build on its own, and each one asks
  # the umbrella for the same set this file hands it here.
  pynixd = import sources.pynixd { inherit pkgs sources; };

  nanopynix = import sources.nanopynix { inherit pkgs sources system; };

  easykubenix = import sources.easykubenix { inherit pkgs sources system; };

  # It builds its own package set, because it applies an overlay of its own,
  # so it takes the sources rather than the set.
  nixkube = import sources.nixkube { inherit sources system; };

  checks = {
    # That every name resolves, and that a working copy wins where there is
    # one. nix/wired.nix says why a source that quietly comes from the wrong
    # place is worse than one that breaks.
    wired = pkgs.callPackage ./nix/wired.nix { inherit sources; };

    # That calling a flake we do not own uses our inputs and not its lock.
    # nix/examples/call-flake.nix is the worked example as well as the gate.
    callFlake = pkgs.callPackage ./nix/examples/call-flake.nix {
      inherit sources inputs pkgs;
      callFlake = callFlake;
    };
  };
}

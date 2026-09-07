{
  pkgs ? import <nixpkgs> { },
}:
rec {
  # umbrella drives this collection. It keeps a submodule commit that no
  # remote has out of the pointers recorded here, and it makes worktreespaces
  # that share storage instead of cloning every repository again.
  #
  # It is a submodule too, so it can be edited in place like the rest. It is
  # also the tool that checks the submodules out, so a clone made without them
  # has to be able to build it anyway: when the directory is not there, fall
  # back to the commit this repository pins.
  umbrella = (import umbrellaSource { inherit pkgs; }).umbrella;

  shell = pkgs.callPackage ./pkgs/shell { inherit umbrella; };

  # The four projects are not composed here yet.
  #
  # Each one carries its own default.nix, its own flake inputs and its own
  # dev shell, and the three signatures do not agree: nanopynix takes
  # `{ inputs, system, pkgs }` through its nix/compat.nix, the others take
  # something else. Wiring them into one package set means agreeing on that
  # signature first, and that is the restructuring the README calls the next
  # phase.
  #
  # Until then each project builds from its own directory, the way it did
  # before it moved in here. What the umbrella gives today is one checkout,
  # one set of pointers and one guard against publishing a pointer nobody
  # can fetch.

  umbrellaSource =
    if builtins.pathExists ./umbrella/default.nix then
      ./umbrella
    else
      pkgs.fetchFromGitHub {
        owner = "Lillecarl";
        repo = "umbrella";
        rev = "2302d8d376a8ce415fe544416958ba24f05922f6";
        hash = "sha256-y/Yownj5+DRPWKo3fATxPxpacNsP0SvKwU0DH483OYE=";
      };
}

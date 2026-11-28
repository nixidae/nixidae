/*
  The dev.nix that examples/web-devenv/README.md shows for your own
  project: the file itself, with this tree's vivarium in place of the
  tarball it fetches. A phase checks that nginx serves its `./public`
  page by name, from the host. Evaluation fails when the README's copy is
  not the file, so the two cannot drift. ../web-devenv-own.py.
*/
{
  lib,
  hostPkgs,
  mkTest,
  ...
}:
let
  file = ../../examples/web-devenv/own/dev.nix;
  readme = builtins.readFile ../../examples/web-devenv/README.md;
  own = (import file { vivarium = { inherit mkTest; }; }).web;
in
assert lib.assertMsg (lib.hasInfix (builtins.readFile file) readme)
  "examples/web-devenv/README.md does not hold examples/web-devenv/own/dev.nix as it is";
own.extend {
  modules = [
    {
      phases.serves = {
        script = ../web-devenv-own.py;
        after = [ "lab" ];
      };
      settings.curl = lib.getExe hostPkgs.curl;
    }
  ];
}

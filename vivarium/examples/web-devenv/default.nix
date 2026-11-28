/*
  A developer environment of plain NixOS guests: a web server you reach
  by name, over HTTPS, from your own shell and browser. See ./README.md.
*/
{
  mkTest,
  pkgs,
  lib,
  hostPkgs ? pkgs,
}:
mkTest {
  name = "web-devenv";

  # Lab names, the proxy that reaches them, and certificates from the lab CA.
  vivarium.recipes.lab.enable = true;

  nodes.web = ./web.nix;

  # Run in the sandbox, this proves the README: what `vivarium shell` and
  # `vivarium browser` reach, from the host.
  phases.check = {
    script = ./check.py;
    after = [ "lab" ];
  };
  settings = {
    curl = lib.getExe hostPkgs.curl;
    publicRoots = "${hostPkgs.cacert}/etc/ssl/certs/ca-bundle.crt";
  };
}

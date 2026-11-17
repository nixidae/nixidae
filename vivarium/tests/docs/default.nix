/*
  Do the docs name commands and library functions that exist?

  Every driver command in a Markdown page parses with the runner's own
  parser, every name of `vivarium.lib` the tree mentions is exported, and
  every exported name has a nixdoc doc comment. No guest boots.
*/
{
  pkgs,
  lib,
  cli,
  libNames,
  ...
}:
let
  root = ../..;
  source = lib.fileset.toSource {
    inherit root;
    fileset = lib.fileset.fileFilter (file: file.hasExt "md" || file.hasExt "nix") root;
  };
in
pkgs.runCommand "vivarium-check-docs"
  {
    names = builtins.toJSON libNames;
    passAsFile = [ "names" ];
  }
  ''
    ${cli}/lib/venv/bin/python ${../checks/docs.py} ${source} "$namesPath"
    touch $out
  ''

/*
  Do the docs name commands and library functions that exist?

  Every driver command in a Markdown page parses with the runner's own
  parser, every name of `vivarium.lib` the tree mentions is exported,
  every exported name has a nixdoc doc comment, and every node of a graph
  under `docs/` names a file and a definition that exist. No guest boots.
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
    # The code too: a graph's node names the file that does its step.
    fileset = lib.fileset.fileFilter (
      file:
      lib.any file.hasExt [
        "md"
        "nix"
        "dot"
        "py"
        "rs"
      ]
    ) root;
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

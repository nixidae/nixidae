# SPDX-License-Identifier: MIT

{ pkgs }:
pkgs.writeScriptBin "nix-csi-validpaths-monitor" ''
  #!${pkgs.python3}/bin/python3
  ${builtins.readFile ./monitor.py}
''

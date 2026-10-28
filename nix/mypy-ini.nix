# That every source's mypy.ini sets `strict = True` and `python_version`
# at its project's requires-python floor (nixidae/nixidae#1113). zuban
# reads mypy's format, which has no `extends`, so each source keeps its own.
{
  lib,
  python3,
  runCommand,
  sources,
}:
let
  members = lib.attrNames (import ./members.nix);
  # A source with no pyproject.toml names its floor here.
  floors = {
    # The oldest CPython in nixpkgs.
    "nixvenv/mypy.ini" = "3.11";
  };
in
runCommand "nixidae-mypy-ini" { } ''
  ${lib.getExe python3} ${./mypy-ini.py} ${
    lib.concatStringsSep " " (
      lib.mapAttrsToList (ini: version: lib.escapeShellArg "--floor=${ini}=${version}") floors
    )
  } ${lib.concatMapStringsSep " " (name: lib.escapeShellArg "${name}=${sources.${name}}") members}
  touch $out
''

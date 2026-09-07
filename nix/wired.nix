# Does every source come from where the umbrella says?
#
# This asks because the failure is quiet. A source that resolves to the
# revision in the lock when a working copy was meant still builds, and it
# builds something that is not what the user is editing. Nothing goes red.
# The first sign of it is a change that seems to have no effect.
#
# So every name gets named and checked, and the answer is a file to read and
# not only an exit code.
{
  lib,
  runCommand,
  # From default.nix: the resolved set.
  sources,
}:
let
  spec = import ./sources.nix;

  # What nix/resolve.nix should have said. The same rule, written again on
  # purpose: a check that shares its implementation checks nothing.
  expected =
    name:
    let
      workingCopy = spec.${name}.path or null;
      present =
        workingCopy != null
        && builtins.pathExists workingCopy
        && builtins.readDir workingCopy != { };
    in
    if present then "working copy" else "lock";

  rows = map (name: rec {
    inherit name;
    have = toString sources.${name};
    want = expected name;
    isWorkingCopy = builtins.substring 0 11 have != "/nix/store/";
    ok = (if isWorkingCopy then "working copy" else "lock") == want && builtins.pathExists have;
  }) (builtins.attrNames spec);

  wrong = builtins.filter (row: !row.ok) rows;

  line = row: "${if row.ok then "ok " else "BAD"}  ${row.name} <- ${row.want}  ${row.have}";
  report = lib.concatMapStringsSep "\n" line rows;
in
if wrong != [ ] then
  throw ''
    the umbrella does not resolve every source the way it says:

    ${lib.concatMapStringsSep "\n    " (row: "${row.name} should come from the ${row.want} and is ${row.have}") wrong}
  ''
else
  runCommand "nixidae-wired"
    {
      inherit report;
      passAsFile = [ "report" ];
    }
    ''
      cp "$reportPath" "$out"
    ''

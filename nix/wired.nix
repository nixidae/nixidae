# Does the umbrella actually own the inputs?
#
# This asks because the failure is silent. When an override does not apply,
# flake-compatish falls back to the project's own flake.lock and the build
# succeeds -- against a published tarball rather than the working copy in the
# next directory. Nothing goes red. The edit somebody just made is simply not
# in what they built, and the first sign of it is a change that seems to have
# no effect.
#
# So every edge gets named and checked. An input the umbrella gives as a
# directory has to arrive as that directory, in the project that declares it.
#
# Only the directories are judged. A flake reference is fetched, and the
# store path it lands on is not something this can predict, so those are
# reported and left alone.
{
  lib,
  runCommand,
  # From default.nix: the wiring itself, and the set it is built from.
  projectInputs,
  inputs,
}:
let
  projects = [
    "nanopynix"
    "pynixd"
    "easykubenix"
    "nixkube"
  ];

  wanted = lib.filterAttrs (_: value: builtins.isPath value) inputs;

  # Only what the project declares. A name it does not ask for is not a
  # missing edge, so `nixidae` itself and the three siblings a project never
  # names drop out here rather than failing.
  check =
    project:
    let
      got = projectInputs project;
      names = builtins.filter (name: got ? ${name}) (builtins.attrNames wanted);
    in
    map (name: rec {
      inherit project name;
      want = toString wanted.${name};
      have = toString got.${name}.outPath;
      ok = want == have;
    }) names;

  rows = lib.concatMap check projects;
  wrong = builtins.filter (row: !row.ok) rows;

  line = row: "${if row.ok then "ok " else "BAD"}  ${row.project} declares ${row.name} -> ${row.have}";
  report = lib.concatMapStringsSep "\n" line rows;
in
if wrong != [ ] then
  throw ''
    the umbrella does not own every input it names:

    ${lib.concatMapStringsSep "\n    " (row: "${row.project} declares ${row.name}, which should be ${row.want} and is ${row.have}") wrong}
  ''
else
  # A run leaves the list behind, so the answer is something to read and not
  # only an exit code.
  runCommand "nixidae-wired"
    {
      inherit report;
      passAsFile = [ "report" ];
    }
    ''
      cp "$reportPath" "$out"
    ''

# Does every source come from where the umbrella says?
#
# This asks because the failure is quiet. A source that resolves to the
# revision in the lock when a working copy was meant still builds, and it
# builds something that is not what the user is editing. Nothing goes red.
# The first sign of it is a change that seems to have no effect.
#
# So every name gets named and checked, and the answer is a file to read and
# not only an exit code.
#
# It reads nix/inputs.nix rather than the fetched set, because that is where
# the three arms are still distinguishable. After nix/fetch.nix two of them
# are the same shape: a store path. Which is the point of the middle arm, and
# also what would make this check blind if it looked there.
{
  lib,
  runCommand,
  # From default.nix: the resolved set, as directories.
  sources,
}:
let
  spec = import ./sources.nix;
  inputs = import ./inputs.nix;

  # What nix/resolve.nix should have said. The same rule, written again on
  # purpose: a check that shares its implementation checks nothing.
  devRequest = builtins.getEnv "UMBRELLA_DEV";
  devAll = builtins.elem devRequest [
    "1"
    "true"
    "all"
  ];
  devNames = builtins.filter (s: builtins.isString s && s != "") (builtins.split "[, ]+" devRequest);
  inDev = name: devRequest != "" && (devAll || builtins.elem name devNames);

  gitRequest = builtins.getEnv "UMBRELLA_GIT";
  gitAll = builtins.elem gitRequest [
    "1"
    "true"
    "all"
  ];
  gitNames = builtins.filter (s: builtins.isString s && s != "") (builtins.split "[, ]+" gitRequest);
  inGit = name: gitRequest != "" && (gitAll || builtins.elem name gitNames);

  # A locked github source that UMBRELLA_GIT names has to be fetched over the
  # git protocol, and not through api.github.com. This is the whole visible
  # effect of that variable, so a check that does not read the reference does
  # not check it at all.
  lock = builtins.fromJSON (builtins.readFile ./sources.lock);
  fromGithub = name: (lock.sources.${name}.type or null) == "github";

  hasWorkingCopy =
    name:
    let
      declared = spec.${name}.path or null;
    in
    declared != null && builtins.pathExists declared && builtins.readDir declared != { };

  want =
    name:
    if hasWorkingCopy name && inDev name then
      "the directory"
    else if hasWorkingCopy name then
      "this checkout, by revision"
    else if inGit name && fromGithub name then
      "the lock, over git"
    else
      "the lock";

  # A locked working copy is fetched from the checkout itself, so the
  # reference names it. That is what makes a build here and a build in CI
  # agree: the same revision either way, and no network for the ones on disk.
  looksRight =
    name: value:
    let
      declared = toString (spec.${name}.path or "");
    in
    {
      "the directory" = builtins.isPath value;
      "this checkout, by revision" =
        builtins.isString value && lib.hasPrefix "git+file://${declared}?" value;
      "the lock, over git" =
        builtins.isString value && lib.hasPrefix "git+https://github.com/" value;
      "the lock" = builtins.isString value && !(lib.hasPrefix "git+file://" value);
    }
    .${want name};

  rows = map (name: rec {
    inherit name;
    have = toString inputs.${name};
    expected = want name;
    ok = looksRight name inputs.${name} && builtins.pathExists (toString sources.${name});
  }) (builtins.attrNames spec);

  wrong = builtins.filter (row: !row.ok) rows;

  line = row: "${if row.ok then "ok " else "BAD"}  ${row.name} <- ${row.expected}  ${row.have}";
  report = lib.concatMapStringsSep "\n" line rows;
in
if wrong != [ ] then
  throw ''
    the umbrella does not resolve every source the way it says:

    ${lib.concatMapStringsSep "\n    " (
      row: "${row.name} should come from ${row.expected} and is ${row.have}"
    ) wrong}
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

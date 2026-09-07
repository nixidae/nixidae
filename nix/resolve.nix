# Join the specification to the lock, and give each name one value.
#
# The rule has two arms, and the first one is the reason this checkout exists:
#
#   A working copy wins. When the spec gives a path and that directory holds
#   a flake.nix, the answer is the path. Nix reads it where it lies, nothing
#   is copied to the store, and an edit in one project reaches the next build
#   of another with no commit, no push and no revision to bump.
#
#   Otherwise the lock answers. The revision in nix/sources.lock becomes a
#   pinned flake reference. It is pure, so an evaluation that never touches a
#   working copy needs no --impure.
#
# The test for a working copy is the flake.nix, not the directory. `git clone`
# without --recurse-submodules leaves every submodule directory present and
# empty, and so does a tarball of this repository. A directory test would pick
# the empty one and fail later with a confusing error.
#
# The value is a path or a flake reference string, because that is what
# flake-compatish takes as an override. Nothing here fetches, so a name a
# project never asks for costs nothing.
{
  spec,
  lock,
}:

let
  # A locked entry, written as a flake reference.
  #
  # The revision alone makes the reference pure -- measured, `nix eval
  # --pure-eval` resolves it for both github and git references. narHash is
  # the integrity check and lets a substituter serve the source instead of
  # the forge. Only "=" needs escaping in the query; "+" and "/" pass
  # through.
  ref =
    name: entry:
    let
      narHash = builtins.replaceStrings [ "=" ] [ "%3D" ] entry.narHash;
      base =
        if entry.type == "github" then
          "github:${entry.owner}/${entry.repo}/${entry.rev}"
        else if entry.type == "git" then
          "git+${entry.url}?rev=${entry.rev}"
        else
          throw "nix/sources.lock: ${name}: unknown source type ${entry.type}";
      separator = if entry.type == "git" then "&" else "?";
    in
    "${base}${separator}narHash=${narHash}";

  resolve =
    name: entrySpec:
    let
      workingCopy = entrySpec.path or null;
      hasWorkingCopy = workingCopy != null && builtins.pathExists (workingCopy + "/flake.nix");
      locked = lock.sources.${name} or null;
    in
    if hasWorkingCopy then
      workingCopy
    else if locked != null then
      ref name locked
    else
      throw "nix/sources.lock has no entry for ${name}. Run `umbrella update`.";
in

assert lock.version == 1;
builtins.mapAttrs resolve spec

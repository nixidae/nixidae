# Join the specification to the lock, and give each name one value.
#
# The rule has three arms, and only one of them diverges:
#
#   A working copy, at the revision the lock names. When the spec gives a path
#   and that directory holds anything, the answer is a `git+file://` reference
#   to that checkout. Nothing reaches the network -- the revision is already
#   on disk -- and the store path is the one CI's cache holds.
#
#   The same working copy, read as a directory, when UMBRELLA_DEV names it.
#   That is what makes an edit reach the next build of another project with
#   no commit and no push, and it is the arm that cannot match a forge.
#
#   Otherwise the lock answers. The revision in nix/sources.lock becomes a
#   pinned flake reference. It is pure, so an evaluation that never touches a
#   working copy needs no --impure.
#
# The test for a working copy is that the directory has something in it, not
# that it is there. A tarball of this repository holds none of them, and an
# `umbrella fetch` that was interrupted can leave one empty. Picking one of
# those gives a confusing error later.
#
# Emptiness and not a marker file, because our repositories are not flakes
# and have no one file they all carry.
#
# The value is a path or a flake reference string, and not a directory,
# because a flake reference is what flake-compatish takes as an override and
# nix/call-flake.nix needs one. nix/fetch.nix turns either into a directory,
# and nix/wire.nix is the set of those. Nothing here fetches, so a name
# nobody asks for costs nothing.
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

  # A working copy, named by revision instead of read as a directory.
  #
  # This is what makes a build here and a build from CI agree. Measured: a
  # `git+file://` fetch of a revision and a `github:` fetch of the same
  # revision give the identical store path, on every repository tried. Reading
  # the directory does not: it is a different input, so every derivation below
  # it differs, and CI's cache holds nothing a local render asks for.
  #
  # The fetch reads the committed tree, so an untracked file in the checkout
  # changes nothing. Measured with a stray `result` symlink in place.
  #
  # Nothing here reaches the network. The revision is already on disk.
  localRef =
    workingCopy: entry:
    let
      narHash = builtins.replaceStrings [ "=" ] [ "%3D" ] entry.narHash;
    in
    "git+file://${toString workingCopy}?rev=${entry.rev}&narHash=${narHash}";

  # UMBRELLA_DEV names the sources to read as directories instead.
  #
  # `1`, `true` or `all` means every one. Anything else is a list of names,
  # separated by commas or spaces.
  #
  # Off by default, and `builtins.getEnv` answers "" in a pure evaluation, so
  # the reproducible arm is what an evaluation gets unless someone asks for
  # the other one. That is the point: the divergence is worth having while you
  # edit, and it has to be something you chose.
  devRequest = builtins.getEnv "UMBRELLA_DEV";
  devAll = builtins.elem devRequest [
    "1"
    "true"
    "all"
  ];
  # `builtins.split` puts the separator matches in the list too, as lists.
  devNames = builtins.filter (s: builtins.isString s && s != "") (builtins.split "[, ]+" devRequest);
  inDev = name: devRequest != "" && (devAll || builtins.elem name devNames);

  resolve =
    name: entrySpec:
    let
      workingCopy = entrySpec.path or null;
      hasWorkingCopy =
        workingCopy != null && builtins.pathExists workingCopy && builtins.readDir workingCopy != { };
      locked = lock.sources.${name} or null;
    in
    # Uncommitted work cannot match anything a forge holds, so this arm has to
    # diverge. It is the only one that does.
    if hasWorkingCopy && inDev name then
      workingCopy
    else if hasWorkingCopy && locked != null then
      localRef workingCopy locked
    else if locked != null then
      ref name locked
    else if hasWorkingCopy then
      # No revision to name it by. A source being added, before the first
      # `umbrella update` writes it down.
      workingCopy
    else
      throw "nix/sources.lock has no entry for ${name}. Run `umbrella update`.";
in

assert lock.version == 1;
builtins.mapAttrs resolve spec

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
# UMBRELLA_GIT changes how that last arm writes a github entry, and nothing
# else. It is the only arm that reaches api.github.com. See `ref` below.
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
  #
  # **A github entry can go over git instead, and UMBRELLA_GIT asks for
  # that.** A `github:` reference costs one api.github.com call, which allows
  # 60 an hour per IP without a token. GitHub's runners share a NAT pool, so
  # strangers spend that budget too, and a wide matrix reaches 60 on its own.
  # A `git+https://` reference of the same repository uses the git protocol,
  # which that limit does not count. See nanopynix issue #301.
  #
  # The lock does not change. The same node writes either reference, because
  # the two fetchers agree on the tree. Measured on this machine: nixpkgs at
  # c7def046 gives sha256-6RSEDHIWQtesQKWSu5qRai8L2h4KgCgMEfJHstW99G4= both
  # ways, which is what nix/sources.lock already holds.
  #
  # **`shallow=1` is not optional.** Nix defaults it to false, so a git fetch
  # of nixpkgs would clone the whole history. Shallow costs one field:
  # `revCount` throws on a shallow repository. Nothing here reads it.
  # Measured: nixpkgs shallow took 8.8 s and 66 MB of ~/.cache/nix/gitv3.
  #
  # **The git arm carries no narHash, and it cannot.** The git scheme lifts
  # `rev`, `ref` and a few flags out of the query and puts every other key
  # back into the repository url (`src/libfetchers/git.cc`, `inputFromURL`).
  # A narHash there becomes part of the address, and the fetch then asks
  # github for a repository named `...?narHash=sha256-...`. Measured: "Failed
  # to fetch git repository". A `git+file://` reference survives the same
  # mistake, because opening a path ignores the query, which is why localRef
  # below still carries one.
  #
  # The revision is the integrity check in its place. A git revision is a
  # hash over the commit, so it pins the tree as the narHash does.
  #
  # **The narHash buys no substitution, so losing it costs nothing.**
  # Measured with a deliberately wrong access token, which answers 401 if a
  # request carried it. On a cold runner -- an empty store and an empty
  # ~/.cache/nix -- `github:NixOS/nixpkgs/<rev>?narHash=<hash>` answers 401.
  # It asks api.github.com first, and it does that even when the store path
  # the narHash names is already valid. The same revision as
  # `git+https://...?shallow=1` answers with the path and never touches the
  # API. What saves the call is the fetcher cache in ~/.cache/nix, and not
  # the store and not the narHash.
  ref =
    name: entry:
    let
      narHash = builtins.replaceStrings [ "=" ] [ "%3D" ] entry.narHash;
    in
    if entry.type == "github" && inGit name then
      "git+https://github.com/${entry.owner}/${entry.repo}?rev=${entry.rev}&shallow=1"
    else if entry.type == "github" then
      "github:${entry.owner}/${entry.repo}/${entry.rev}?narHash=${narHash}"
    else if entry.type == "git" then
      "git+${entry.url}?rev=${entry.rev}&narHash=${narHash}"
    else
      throw "nix/sources.lock: ${name}: unknown source type ${entry.type}";

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

  # The sources that one environment variable names.
  #
  # `1`, `true` or `all` means every one. Anything else is a list of names,
  # separated by commas or spaces.
  #
  # Off by default, and `builtins.getEnv` answers "" in a pure evaluation, so
  # an evaluation gets the plain arm unless someone asks for the other one.
  # `nix build --file .` honours the variable and a flake consumer never sees
  # it, which is true of both variables below.
  selectedBy =
    var:
    let
      request = builtins.getEnv var;
      all = builtins.elem request [
        "1"
        "true"
        "all"
      ];
      # `builtins.split` puts the separator matches in the list too, as lists.
      names = builtins.filter (s: builtins.isString s && s != "") (builtins.split "[, ]+" request);
    in
    name: request != "" && (all || builtins.elem name names);

  # The sources one environment variable names *by name*, never the `all`
  # forms. Asking for everything is a blanket request, so a name it cannot
  # serve is not a mistake. Writing one name down is a specific request.
  namedBy =
    var:
    let
      request = builtins.getEnv var;
      names = builtins.filter (s: builtins.isString s && s != "") (builtins.split "[, ]+" request);
    in
    name: builtins.elem name names;

  # UMBRELLA_DEV names the sources to read as directories instead.
  #
  # That is what makes an edit reach the next build of another project with no
  # commit and no push. The divergence is worth having while you edit, and it
  # has to be something you chose.
  inDev = selectedBy "UMBRELLA_DEV";
  namedDev = namedBy "UMBRELLA_DEV";

  # UMBRELLA_GIT names the sources to fetch over git instead of over the
  # GitHub API. It changes the reference and never the result: see `ref`.
  inGit = selectedBy "UMBRELLA_GIT";

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
    else if namedDev name then
      # Named in UMBRELLA_DEV, and there is no working copy to read. Falling
      # through to the lock would answer with the pinned source, which is the
      # opposite of what was asked for and says nothing about it.
      #
      # This is how a before/after measurement reports a null: both arms run
      # the locked revision, the output is byte-identical, and the difference
      # is zero. Measured 2026-09-17, on an umbrella that was itself a store
      # path and therefore had no sibling directories at all.
      throw ''
        UMBRELLA_DEV names ${name}, and there is no working copy to read.

        Looked for: ${
          if workingCopy == null then "nothing -- the spec gives no path" else toString workingCopy
        }

        The directory is missing or empty. An umbrella that is itself a
        store path has no working copies beside it, so UMBRELLA_DEV can
        do nothing there.

        Run `umbrella fetch`, or drop ${name} from UMBRELLA_DEV.
      ''
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

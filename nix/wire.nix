# Every source, as a directory: the ten in this tree, and each pin of
# `flake.lock`.
#
# A source's own `nix/sources.nix` calls this, here and in its exported
# repository. There it passes `overrides.<name> = ../.`, so the source builds
# from its own tree against its siblings at the nixidae commit its lock names.
#
# Each member is a store path of its own, named `source`, holding what git
# tracks. That is what a fetch of the member's own repository gives, so a
# build here, a build in CI and a build of the exported repository agree,
# and a change in one member does not change another's source.
#
# In a checkout the tracked tree comes from `fetchGit` of the working copy:
# uncommitted edits count, and so does a new file, because jj marks it
# intent-to-add in git's index. A `result` link or a local scratch file does
# not, since git ignores it. A fetched tree has no `.git`, and is already
# what git tracks.
#
# A flake we do not own is called through nix/call-flake.nix, the only place
# a flake is read.
{
  inputs ? import ./compat.nix,
  overrides ? { },
}:
let
  # A jj workspace that is not colocated has `.jj` and no `.git`. Nix cannot
  # read what jj tracks there, and the plain path would take ignored files too.
  tree =
    if builtins.pathExists ../.git then
      (builtins.fetchGit { url = toString ../.; }).outPath
    else if builtins.pathExists ../.jj then
      throw "nix/wire.nix: ${toString ../.} is a jj workspace without .git, so Nix cannot read what jj tracks in it. Run `jj git colocation enable` there (jj >= 0.46). nixidae/nixidae#18"
    else
      ../.;

  members = builtins.mapAttrs (
    name: _:
    builtins.path {
      path = "${tree}/${name}";
      name = "source";
    }
  ) (import ./members.nix);

  # An ignored checkout of one of these, beside the sources, takes the place
  # of its pin, so an edit there reaches the next build. What git tracks in
  # it counts, as for a member. A clone or a fetched tree has none, and the
  # lock answers.
  checkouts = [
    "containerd"
    "flake-compatish"
    "kr8s"
  ];
  checkout =
    name:
    let
      dir = ../. + "/${name}";
    in
    builtins.elem name checkouts && builtins.pathExists dir && builtins.readDir dir != { };

  pinned = builtins.mapAttrs (
    name: input:
    if checkout name then
      (builtins.fetchGit { url = toString ../. + "/${name}"; }).outPath
    else
      input.outPath
  ) (removeAttrs inputs [ "self" ]);
in
pinned // members // overrides

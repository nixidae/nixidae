# Give a project the sources the umbrella owns.
#
# This is the seam every project enters through. Inside the umbrella,
# default.nix calls it. Outside, a project's own default.nix fetches the
# umbrella and calls it, so the umbrella decides the sources either way and
# there is only one answer to keep right.
#
# What comes back is a set of directories, one per name in nix/sources.nix.
# Nothing is evaluated here: a project imports what it wants, the way it
# wants. A flake we do not own is called through nix/call-flake.nix, and
# that is the only place a flake is read at all.
{
  inputs ? import ./inputs.nix,

  # A working copy to use in place of one of the names.
  #
  # A project that pulled the umbrella in from outside passes its own
  # checkout here. Without it the umbrella's own copy would win, and the
  # user's edits would go nowhere.
  overrides ? { },
}:
builtins.mapAttrs (_: import ./fetch.nix) (inputs // overrides)

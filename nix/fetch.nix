# Turn one value from nix/inputs.nix into a directory.
#
# nix/resolve.nix gives a path for a working copy and a flake reference
# string for a locked source. Almost every caller wants one shape, and this
# is where the two become one. nix/wire.nix maps it over the whole set.
#
# A working copy stays the path it is. Nothing is copied to the store, which
# is what lets an edit in one repository reach the next build of another.
#
# A string that already names a store path is one too, and it is returned as
# it is. It arrives when a caller overrides a name with something it has
# already fetched -- a project's own flake.nix does exactly that with
# `nixpkgs.outPath`. `parseFlakeRef` cannot take it: a pure evaluation
# refuses a string that carries store-path context, and says
#
#   error: the string '/nix/store/...' is not allowed to refer to a store
#          path
value:
if builtins.isPath value then
  value
else if builtins.substring 0 11 value == "/nix/store/" then
  value
else
  (builtins.fetchTree (builtins.parseFlakeRef value)).outPath

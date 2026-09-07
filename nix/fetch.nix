# Turn one value from nix/inputs.nix into something `import` takes.
#
# nix/resolve.nix gives a path for a working copy and a flake reference
# string for a locked source. Almost every caller wants one shape, and this
# is where the two become one. nix/wire.nix maps it over the whole set.
#
# A working copy stays the path it is. Nothing is copied to the store, which
# is what lets an edit in one repository reach the next build of another.
value:
if builtins.isPath value then value else (builtins.fetchTree (builtins.parseFlakeRef value)).outPath

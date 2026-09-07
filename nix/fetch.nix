# Turn one value from nix/inputs.nix into something `import` takes.
#
# nix/resolve.nix gives a path for a working copy and a flake reference
# string for a locked source. flake-compatish takes either, so most callers
# hand the value straight over. A caller that has to read the source itself
# needs one shape, and this is where the two become one.
value:
if builtins.isPath value then value else (builtins.fetchTree (builtins.parseFlakeRef value)).outPath

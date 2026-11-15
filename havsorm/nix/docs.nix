{
  callPackage,
  nixvenv,
}:
callPackage ./shared/sphinx-site.nix { inherit nixvenv; } {
  name = "havsorm";
  root = ../.;
  # `docs/usage.md` includes the README.
  files = [ ../README.md ];
}

{
  callPackage,
  nixvenv,
}:
callPackage ./shared/sphinx-site.nix { inherit nixvenv; } {
  name = "kappy";
  root = ../.;
  # `docs/usage.md` includes the README.
  files = [ ../README.md ];
}

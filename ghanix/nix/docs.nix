{
  callPackage,
  nixvenv,
  optionsReference,
}:
callPackage ./shared/sphinx-site.nix { inherit nixvenv; } {
  name = "ghanix";
  root = ../.;
  # `docs/usage.md` includes the README.
  files = [ ../README.md ];
  pages."options.md" = optionsReference;
}

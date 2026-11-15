{
  callPackage,
  nixvenv,
}:
callPackage ./shared/sphinx-site.nix { inherit nixvenv; } {
  name = "huggorm";
  root = ../.;
}

{
  callPackage,
  nixvenv,
  testOptions,
  guestOptions,
}:
callPackage ./shared/sphinx-site.nix { inherit nixvenv; } {
  name = "vivarium";
  root = ../.;
  pages = {
    "test-options.md" = testOptions;
    "guest-options.md" = guestOptions;
  };
}

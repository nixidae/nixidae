{
  callPackage,
  graphviz,
  nixvenv,
  testOptions,
  guestOptions,
}:
callPackage ./shared/sphinx-site.nix { inherit nixvenv; } {
  name = "vivarium";
  root = ../.;
  tools = [ graphviz ];
  pages = {
    "test-options.md" = testOptions;
    "guest-options.md" = guestOptions;
  };
}

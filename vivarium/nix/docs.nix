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
  # docs/examples.md includes each example's README.
  files = [
    ../examples/web-devenv/README.md
    ../examples/k8s-devenv/README.md
  ];
  pages = {
    "test-options.md" = testOptions;
    "guest-options.md" = guestOptions;
  };
}

/*
  openSUSE with a writable /nix (`vivarium.image.writableStore`):
  Nix there opens a local store whose database knows the guest's
  closure, and a collection keeps the agent.
*/
{
  pkgs,
  vivarium,
  mkTest,
  images,
  ...
}:
mkTest {
  name = "suse-nix";
  settings.nix = "${pkgs.nix}";
  settings.hello = "${pkgs.hello}";
  nodes.suse =
    { settings, ... }:
    {
      imports = [ vivarium.kinds.image ];
      # Not in the image's own roots: the runner registers it after boot.
      vivarium.lateClosure = [ settings.hello ];
      vivarium.image = images.opensuse-leap-16_0 // {
        writableStore = true;
      };
      vivarium.memory = "1024M";
    };
  phases.nix = {
    script = ../phases/suse-nix.py;
    after = [ "boot" ];
  };
}

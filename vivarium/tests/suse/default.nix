/*
  Does openSUSE Leap boot under UML, with the agent in it?

  The image is SUSE's own cloud image; the agent, the journal stream
  and /artifacts come from a cloud-init seed. See
  modules/image-guest.nix.
*/
{
  pkgs,
  nixvenv,
  vivarium,
  mkTest,
  images,
  ...
}:
mkTest {
  name = "suse";
  # A closure the guest does not boot with, for `add_closure`. Not
  # plain python3: the agent runs on it, so the guest has it already.
  settings.python =
    let
      env = nixvenv {
        python = pkgs.python3;
        packages = ps: [ ps.requests ];
        executables = [ "python3" ];
      };
    in
    {
      closure = "${pkgs.closureInfo { rootPaths = [ env ]; }}";
      bin = "${env}/bin/python3";
    };
  nodes.suse = {
    imports = [ vivarium.kinds.image ];
    vivarium.image = images.opensuse-leap-16_0;
    vivarium.memory = "1024M";
  };
  phases.suse = {
    script = ../phases/suse.py;
    after = [ "boot" ];
  };
}

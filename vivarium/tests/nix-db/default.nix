{ pkgs, mkTest, ... }:
mkTest {
  name = "nix-db";
  nodes.one =
    { pkgs, ... }:
    {
      environment.systemPackages = [ pkgs.sqlite ];
    };
  phases.nix-db = {
    script = ../phases/nix-db.py;
    after = [ "boot" ];
  };
}

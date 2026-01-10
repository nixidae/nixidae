{
  pkgs,
  ...
}:
{
  config = {
    users = {
      enable = true;
      users.root = {
        shell = pkgs.runtimeShell;
        homeDir = "/nix/var/nix-csi/root";
      };
      users.nix = {
        uid = 1000;
        gid = 1000;
        comment = "Nix worker user";
      };
      groups.nix.gid = 1000;
      groups.nixbld.gid = 30000;
      users.sshd = {
        uid = 993;
        gid = 992;
        comment = "SSH privilege separation user";
      };
      groups.sshd.gid = 992;
    };
  };
}

/*
  Can a guest host a userspace filesystem?

  The question a build sandbox cannot answer for itself: its /dev has
  null, zero, random and little else, so a FUSE mount is out of reach
  there however the test is written.  A guest brings its own kernel
  and so its own /dev/fuse.

  Unprivileged mounting takes programs.fuse, which is opt in on NixOS
  and is what puts a setuid fusermount3 under /run/wrappers.  The
  wrappers themselves a guest already has.
*/
{ pkgs, mkTest, ... }:
mkTest {
  name = "fuse";
  phases.test = {
    script = ../fuse.py;
    after = [ "boot" ];
  };
  nodes.node = {
    programs.fuse.enable = true;
    programs.fuse.userAllowOther = true;
    environment.systemPackages = [
      pkgs.bindfs
      pkgs.util-linux
    ];
    users.users.alice = {
      isNormalUser = true;
      uid = 1000;
    };
  };
}

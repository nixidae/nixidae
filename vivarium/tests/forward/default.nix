/*
  Can the host reach a service in a guest?

  The only test that connects inwards.  One guest with every
  port forwarded, and a web server started long after passt
  stopped accepting arguments -- which is the case that cannot
  be checked any other way, since passt's forwards are fixed for
  its lifetime.
*/
{ pkgs, mkTest, ... }:
mkTest {
  name = "forward";
  phases.test = {
    script = ../forward.py;
    after = [ "boot" ];
  };
  nodes.node = {
    vivarium.forward = [ { ports = "all"; } ];
    environment.systemPackages = [ pkgs.python3 ];
  };
}

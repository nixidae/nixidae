/*
  Router networks (nixidae/nixidae#1523): the router is a member of two
  segments, `lab` and `other`, each a network of its own in a VRF. `d`
  and `x` lease their addresses there and reach the host through the
  router's relay for their network; nothing crosses the router between
  the networks or into the uplink. `m` is on the uplink only, with
  forwards on the host's 127.0.0.1, as every forward has on macOS: the
  uplink's gateway reaches them, and the networks' flows do not.
  router-network.py.
*/
{ mkTest, ... }:
let
  answer =
    { pkgs, ... }:
    {
      environment.systemPackages = [ pkgs.socat ];
    };
in
mkTest {
  name = "router-network";
  segments.lab.router = {
    address = "10.9.0.1/24";
    address6 = "fd00:9::1/64";
  };
  segments.other.router.address = "10.8.0.1/24";
  nodes.m =
    { config, ... }:
    {
      imports = [ answer ];
      # A build binds any free host port; router-network.py asks `reachable`.
      vivarium.forward = [
        { ports = [ config.vivarium.sshPort ]; }
        {
          address = "127.0.0.1";
          ports = [
            {
              host = 47777;
              guest = 7777;
            }
          ];
        }
        {
          address = "127.0.0.1";
          ports = [
            {
              host = 47778;
              guest = 7778;
            }
          ];
          protocols = [ "udp" ];
        }
      ];
    };
  nodes.d = {
    imports = [ answer ];
    vivarium.interfaces.lab.segment = "lab";
  };
  nodes.x = {
    imports = [ answer ];
    vivarium.interfaces.other.segment = "other";
  };
  phases.networks = {
    script = ../router-network.py;
    after = [ "boot" ];
  };
}

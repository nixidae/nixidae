/*
  Does every guest import `defaults`, and know each peer by name?

  No guest names another in its own configuration: /etc/hosts comes
  from the `nodes` every guest receives, as in nixos-test.
*/
{ mkTest, ... }:
mkTest {
  name = "peers";
  defaults.environment.etc."vivarium-defaults".text = "from-defaults\n";
  nodes.server.vivarium.lan = {
    network = "peers";
    address = "192.168.99.2/24";
  };
  nodes.client.vivarium.lan = {
    network = "peers";
    address = "192.168.99.3/24";
  };
  phases.peers = {
    script = ../phases/peers.py;
    after = [ "boot" ];
  };
}

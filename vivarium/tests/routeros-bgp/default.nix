/*
  eBGP between RouterOS and FRR. Each announces its LAN, and a host on
  one LAN reaches a host on the other only through the routes BGP
  gave the routers: two hops.
*/
{
  vivarium,
  mkTest,
  images,
  lanHost,
  frrPeer,
  ...
}:
mkTest {
  name = "routeros-bgp";
  backend = "qemu";
  nodes.r1 = {
    vivarium.interfaces = {
      core = {
        segment = "core";
        addresses = [ "10.0.0.1/30" ];
      };
      west = {
        segment = "west";
        addresses = [ "10.1.0.1/24" ];
      };
    };
    imports = [ vivarium.kinds.routeros ];
    vivarium.routeros = images.routeros-chr // {
      config = ''
        /ip firewall address-list add list=bgp-out address=10.1.0.0/24
        /routing bgp instance add name=r1 as=65001 router-id=10.0.0.1
        /routing bgp connection add name=f1 instance=r1 local.role=ebgp remote.address=10.0.0.2 remote.as=65002 output.network=bgp-out hold-time=3s keepalive-time=1s
      '';
    };
  };
  nodes.f1 = frrPeer;
  nodes.a = lanHost "west" "10.1.0.2" "10.1.0.1" "10.2.0.0";
  nodes.b = lanHost "east" "10.2.0.2" "10.2.0.1" "10.1.0.0";
  phases.bgp = {
    script = ../phases/routeros-bgp.py;
    after = [ "boot" ];
  };
}

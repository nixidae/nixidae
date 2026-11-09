/*
  VyOS management VRF on the out-of-band fabric (nixidae/nixidae#958),
  the VyOS analogue of oob-routeros: eth0 (the uplink) leases in a
  `mgmt` VRF, and the dataplane (eth1) stays in the default table. The
  two routing tables are separate, so management and dataplane cannot
  reach each other -- the isolation sonic-vs could not instantiate,
  here with VyOS's real VRFs.
*/
{
  vivarium,
  mkTest,
  images,
  lanHost,
  servicesNode,
  ...
}:
mkTest {
  name = "vyos-oob-vrf";
  nodes.svc = servicesNode { segment = "mgmt"; };
  nodes.vyos = {
    vivarium.uplink.segment = "mgmt";
    vivarium.interfaces.eth1 = {
      segment = "data";
      addresses = [ "10.9.0.1/24" ];
    };
    vivarium.memory = "1024M";
    imports = [ vivarium.kinds.vyos ];
    vivarium.vyos = images.vyos-rolling // {
      uplinkVrf = "mgmt";
      config = ''
        system {
            host-name "vyos-vrf"
        }
      '';
    };
  };
  nodes.d1 = lanHost "data" "10.9.0.2" "10.9.0.1" "10.42.0.0";
  phases.vrf = {
    script = ../phases/vyos-oob-vrf.py;
    after = [ "boot" ];
  };
}

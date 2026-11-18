/*
  VyOS management VRF (nixidae/nixidae#958, #1523), the VyOS analogue of
  oob-routeros: eth0, the uplink, leases from the run's router, which is
  the management switch, in a `mgmt` VRF; the dataplane (eth1) stays in
  the default table. The two routing tables are separate, so management
  and dataplane cannot reach each other -- the isolation sonic-vs could
  not instantiate, here with VyOS's real VRFs. `svc` is a management host
  on the router's uplink.
*/
{
  vivarium,
  mkTest,
  images,
  ...
}:
mkTest {
  name = "vyos-oob-vrf";
  router.enable = true;
  nodes.svc = { };
  nodes.vyos = {
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
  nodes.d1.vivarium.interfaces.data = {
    segment = "data";
    addresses = [ "10.9.0.2/24" ];
  };
  phases.vrf = {
    script = ../phases/vyos-oob-vrf.py;
    after = [ "boot" ];
  };
}

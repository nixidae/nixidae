/*
  VyOS on the out-of-band fabric (nixidae/nixidae#958): VyOS's uplink
  (eth0) is on the management segment and leases from the services
  guest's Kea. The vyos-guest module renames vec0 to eth0 and DHCPs it
  when `uplink.segment` is set. VyOS reaches the fabric the real way,
  not over a static address nobody hands out.
*/
{
  vivarium,
  mkTest,
  images,
  servicesNode,
  ...
}:
mkTest {
  name = "vyos-oob";
  nodes.svc = servicesNode { segment = "mgmt"; };
  nodes.vyos = {
    vivarium.uplink.segment = "mgmt";
    vivarium.memory = "1024M";
    imports = [ vivarium.kinds.vyos ];
    vivarium.vyos = images.vyos-rolling // {
      config = ''
        system {
            host-name "vyos-oob"
        }
      '';
    };
  };
  phases.oob = {
    script = ../phases/vyos-oob.py;
    after = [ "boot" ];
  };
}

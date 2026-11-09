/*
  RouterOS management VRF on the OOB fabric (nixidae/nixidae#958):
  ether1 (the uplink) is on the management segment, and the dataplane
  is a separate interface. Proves functional mgmt/dataplane isolation
  with a VRF, the model sonic-vs could not instantiate.
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
  name = "oob-routeros";
  backend = "qemu";
  nodes.svc = servicesNode { segment = "mgmt"; };
  nodes.r1 = {
    imports = [ vivarium.kinds.routeros ];
    vivarium.routeros = images.routeros-chr // {
      # ether1 is the uplink on the management segment; put it in a
      # mgmt VRF. The dataplane interface stays in `main`, so the two
      # have separate routing tables.
      config = ''
        /ip vrf add name=mgmt interfaces=ether1
      '';
    };
    vivarium.uplink.segment = "mgmt";
    vivarium.interfaces.data = {
      segment = "data";
      addresses = [ "10.9.0.1/24" ];
    };
  };
  nodes.d1 = lanHost "data" "10.9.0.2" "10.9.0.1" "10.42.0.0";
  phases.vrf = {
    script = ../phases/oob-routeros.py;
    after = [ "boot" ];
  };
}

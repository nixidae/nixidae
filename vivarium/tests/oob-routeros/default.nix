/*
  RouterOS management VRF (nixidae/nixidae#958, #1523): ether1, the
  uplink, leases from the run's router, which is the management switch,
  and sits in a `mgmt` VRF; the dataplane is a separate interface. Proves
  functional mgmt/dataplane isolation with a VRF, the model sonic-vs could
  not instantiate. `svc` is a management host on the router's uplink.
*/
{
  vivarium,
  mkTest,
  images,
  ...
}:
mkTest {
  name = "oob-routeros";
  backend = "qemu";
  router.enable = true;
  nodes.svc = { };
  nodes.r1 = {
    imports = [ vivarium.kinds.routeros ];
    vivarium.routeros = images.routeros-chr // {
      # ether1 is the uplink on the management network; put it in a
      # mgmt VRF. The dataplane interface stays in `main`, so the two
      # have separate routing tables.
      config = ''
        /ip vrf add name=mgmt interfaces=ether1
      '';
    };
    vivarium.interfaces.data = {
      segment = "data";
      addresses = [ "10.9.0.1/24" ];
    };
  };
  nodes.d1.vivarium.interfaces.data = {
    segment = "data";
    addresses = [ "10.9.0.2/24" ];
  };
  phases.vrf = {
    script = ../phases/oob-routeros.py;
    after = [ "boot" ];
  };
}

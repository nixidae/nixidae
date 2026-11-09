/*
  Provision SONiC the real way (nixidae/nixidae#958): SONiC's eth0 is
  its uplink on the management segment and leases from the services
  guest's Kea; the services guest then reaches SONiC over SSH and
  applies configuration. No console beyond the boot login.
*/
{
  pkgs,
  vivarium,
  mkTest,
  images,
  servicesNode,
  ...
}:
mkTest {
  name = "oob-sonic";
  backend = "qemu";
  nodes.svc = {
    imports = [ (servicesNode { segment = "mgmt"; }) ];
    environment.systemPackages = [
      pkgs.sshpass
      pkgs.jq
    ];
  };
  nodes.sw1 = {
    imports = [ vivarium.kinds.sonic ];
    vivarium.sonic = images.sonic-vs;
    vivarium.uplink.segment = "mgmt";
  };
  phases.provision = {
    script = ../phases/oob-sonic.py;
    after = [ "boot" ];
  };
  # The heavier path, on the same booted SONiC: push a whole
  # config_db.json and apply it with `config reload`.
  phases.reload = {
    script = ../phases/oob-sonic-reload.py;
    after = [ "provision" ];
  };
}

/*
  Provision SONiC the real way (nixidae/nixidae#958, #1523): SONiC's eth0
  is its uplink and leases from the run's router, the management switch;
  the services guest, on the same switch, reaches SONiC over SSH and
  applies configuration. No console beyond the boot login.
*/
{
  pkgs,
  vivarium,
  mkTest,
  images,
  ...
}:
mkTest {
  name = "oob-sonic";
  backend = "qemu";
  router.enable = true;
  nodes.svc.environment.systemPackages = [
    pkgs.sshpass
    pkgs.jq
  ];
  nodes.sw1 = {
    imports = [ vivarium.kinds.sonic ];
    vivarium.sonic = images.sonic-vs;
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

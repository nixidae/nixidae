/*
  One guest as a rootless container under crun (Area 8 of the design).

  `nix build --file . checks.container` needs a daemon with the `uid-range`
  feature; CI's `test-container` gets it from ghanix's
  `nix.install.uidRange`. There the guest has a read-only store,
  and the phase says so. By hand, `vivarium run checks.container`, it has
  a writable one;
  the host needs subordinate ids and a cgroup it can delegate, and
  the runner says which is missing.
*/
{ mkTest, ... }:
mkTest {
  name = "container";
  nodes.one =
    { config, ... }:
    {
      vivarium.backend = "container";
      vivarium.inbound = [ { guest = config.vivarium.sshPort; } ];
    };
  phases.check = {
    script = ../phases/container.py;
    after = [ "boot" ];
  };
}

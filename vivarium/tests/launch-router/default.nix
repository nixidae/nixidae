# Do a template's instances sit behind the run's router? Each takes the
# router's lease for its index on `vec0`, and its own IPv6 address.
{ mkTest, ... }:
mkTest {
  name = "launch-router";
  router.enable = true;
  nodes.base = { };
  templates.worker = { };
  phases.test = {
    script = ../launch-router.py;
    after = [ "boot" ];
  };
}

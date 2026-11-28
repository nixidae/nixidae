/*
  `vivarium.recipes.lab` beyond the example (nixidae/nixidae#1669).

  examples/web-devenv proves the host's view. This proves the guests':
  `client` reaches `web` by its lab names through `/etc/hosts`, trusting
  the run's lab root. And the units in `vivarium.lab.tls.units` come
  back by themselves: started again from the path unit, as at a reboot
  with the key on disk, and kept through `switch-to-configuration`.
  ../lab.py.
*/
{
  pkgs,
  mkTest,
  ...
}:
mkTest {
  name = "lab";
  vivarium.recipes.lab.enable = true;
  nodes.web = ../../examples/web-devenv/web.nix;
  nodes.client.environment.systemPackages = [ pkgs.curl ];
  phases.guests = {
    script = ../lab.py;
    after = [ "lab" ];
  };
}

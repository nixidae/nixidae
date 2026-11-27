/*
  Does a browser that `vivarium browser` prepares reach a lab page by its
  name, over HTTPS with the lab CA? nixidae/nixidae#1656.

  `web` serves https://web.test/ with a certificate the run's intermediate
  signs, and answers DNS for `test`; the router's SOCKS proxy resolves
  `test` there. The `fetch` phase prepares Chromium as the command does
  (vivarium/browser.py: the split proxy, the lab root in its NSS
  database) and loads the page headless. Negative control: the same
  Chromium without the lab root is refused. tests/browser.py.

  `vivarium up checks.browser` keeps it up for `vivarium browser browser`.
*/
{
  mkTest,
  pkgs,
  lib,
  ...
}:
let
  address = node: node.vivarium.router.address node.vivarium.index;
in
mkTest {
  name = "browser";
  router.enable = true;
  nodes = {
    router =
      { nodes, ... }:
      {
        vivarium.router.socks = {
          enable = true;
          domains."test" = [ (address nodes.web) ];
        };
      };
    web =
      { config, ... }:
      let
        here = address config;
      in
      {
        services.dnsmasq = {
          enable = true;
          resolveLocalQueries = false;
          settings = {
            address = "/web.test/${here}";
            # Answers for `test` itself: "no such record", never a refusal.
            local = "/test/";
            listen-address = here;
            # Binds when the address comes up: dnsmasq can start before vec0 has it.
            bind-dynamic = true;
            no-resolv = true;
          };
        };
        # Started by the `serve` phase, once the certificate is in /artifacts.
        systemd.services.web-https = {
          serviceConfig.ExecStart = "${lib.getExe pkgs.python3} ${./web-https.py} /artifacts/tls";
        };
        networking.firewall = {
          allowedTCPPorts = [
            53
            443
          ];
          allowedUDPPorts = [ 53 ];
        };
      };
  };
  settings = {
    chromium = lib.getExe pkgs.chromium;
    certutil = lib.getExe' pkgs.nss.tools "certutil";
  };
  phases = {
    serve = {
      script = ../browser.py;
      after = [ "boot" ];
    };
    fetch = {
      script = ../browser-fetch.py;
      after = [ "serve" ];
    };
  };
}

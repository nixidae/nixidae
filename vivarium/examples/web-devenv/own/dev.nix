{
  vivarium ?
    (import (builtins.fetchTarball "https://github.com/nixidae/vivarium/archive/main.tar.gz") { }).lib,
}:
{
  web = vivarium.mkTest {
    name = "web";
    vivarium.recipes.lab.enable = true;
    nodes.web =
      { config, ... }:
      {
        vivarium.lab.tls = {
          enable = true;
          units = [ "nginx" ];
        };
        services.nginx = {
          enable = true;
          virtualHosts."web.lab.test" = {
            onlySSL = true;
            sslCertificate = "${config.vivarium.lab.tls.directory}/cert.pem";
            sslCertificateKey = "${config.vivarium.lab.tls.directory}/key.pem";
            root = ./public;
          };
        };
        networking.firewall.allowedTCPPorts = [ 443 ];
      };
  };
}

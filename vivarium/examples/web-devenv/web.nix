# The web guest: nginx serves web.lab.test on 443 and api.lab.test on
# 8443, with a certificate from the run's lab CA.
{ config, ... }:
let
  tls = config.vivarium.lab.tls.directory;
  page = text: {
    onlySSL = true;
    sslCertificate = "${tls}/cert.pem";
    sslCertificateKey = "${tls}/key.pem";
    locations."/" = {
      return = "200 '${text}\\n'";
      # A browser shows it; nginx's default type makes it a download.
      extraConfig = "default_type text/plain;";
    };
  };
in
{
  # web.lab.test is the guest's own name. api.lab.test is a second one,
  # served on 8443: its SRV record takes https://api.lab.test/ there.
  vivarium.lab.names.api.ports.https = 8443;
  # A certificate for both names. nginx starts once it is in place.
  vivarium.lab.tls = {
    enable = true;
    units = [ "nginx" ];
  };

  services.nginx = {
    enable = true;
    virtualHosts."web.lab.test" = page "hello from web.lab.test";
    virtualHosts."api.lab.test" = page "hello from the api on 8443" // {
      listen = [
        {
          addr = "0.0.0.0";
          port = 8443;
          ssl = true;
        }
      ];
    };
  };
  networking.firewall.allowedTCPPorts = [
    443
    8443
  ];
}

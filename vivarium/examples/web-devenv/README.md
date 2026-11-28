# A web developer environment of NixOS guests

One NixOS guest runs nginx. You reach it from your own shell and your
own browser by name, over HTTPS:

- `https://web.lab.test/`: the guest's own name, on port 443.
- `https://api.lab.test/`: a second name, served on port 8443. You still
  type no port.

You need no port forward, no `/etc/hosts` line and no change to your
trust store. `checks.web-devenv` runs this example in the Nix sandbox
and proves each command below from the host. `checks.lab` proves the
guests' side: another guest reaches both names, and nginx comes back by
itself after a restart and a switch.

## The parts

| File | Line | What it gives |
| --- | --- | --- |
| `default.nix` | `vivarium.recipes.lab.enable = true` | A router guest with a SOCKS5 proxy that answers `<guest>.lab.test` for every guest, and a CA for the run under your own lab root. |
| `web.nix` | `vivarium.lab.names.api.ports.https = 8443` | The name `api.lab.test`, and an SRV record that sends `https://api.lab.test/` to port 8443. |
| `web.nix` | `vivarium.lab.tls = { enable = true; units = [ "nginx" ]; }` | A certificate for `web.lab.test` and `api.lab.test` in `/var/lib/vivarium/tls`. nginx starts once it is in place. |
| `default.nix` | `phases.check` | `check.py`: the commands below, run from the host. |

The recipe's `lab` phase runs after the guests boot. It waits until each
guest holds its lab address, then writes the certificates. A phase of
yours that needs names or certificates runs `after = [ "lab" ]`.

## Use it

From the `vivarium` directory of nixidae:

```console
$ vivarium up checks.web-devenv
$ vivarium shell web-devenv
$ curl https://web.lab.test/
hello from web.lab.test
$ curl https://api.lab.test/
hello from the api on 8443
$ exit
$ vivarium browser web-devenv --url web.lab.test --url api.lab.test
$ vivarium down web-devenv
```

`vivarium shell` sets the proxy variables and `SSL_CERT_FILE` for the
run. `vivarium browser` starts your default browser with a profile of
its own that uses the proxy and trusts the lab root. It leaves your own
profile alone. Both work only while the environment is up.

## In your own project

Put the environment in a file of your project, such as `dev.nix`. This
one serves `./public` at `https://web.lab.test/`:

```nix
let
  vivarium = (import (builtins.fetchTarball "https://github.com/nixidae/vivarium/archive/main.tar.gz") { }).lib;
in
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
```

Then:

```console
$ vivarium up web --file dev.nix
$ vivarium browser web --url web.lab.test
$ vivarium down web
```

`nix build --file dev.nix web` runs the same environment in the Nix
sandbox, as CI does, and passes when every phase passes.

## Limits

- Only programs that use the proxy reach the lab: curl, Go and Python
  programs, browsers. `vivarium shell` sets the variables they read.
- An SRV record helps proxy clients alone. Inside the lab, a guest
  names the port: `https://api.lab.test:8443/`.
- A guest the run adds at run time, from a template, has no name.
- The domain ends in `.test` or `.internal`: the lab CA signs nothing
  else.

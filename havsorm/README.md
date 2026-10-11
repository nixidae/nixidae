# havsorm

Salt states written in Nix. havsorm is a Salt extension that adds a
`#!nix` renderer. huggorm evaluates the SLS file in the Salt process, so
a minion needs no Nix daemon and no `nix` command.

## An SLS file in Nix

The file is an attribute set of states:

    #!nix
    {
      hello."pkg.installed" = [ { name = "hello"; } ];
    }

Or a function that takes what Salt knows about the minion:

    #!nix
    { grains, pillar, saltenv, sls, ... }:
    {
      "motd-${grains.id}"."file.managed" = [
        { name = "/etc/motd"; contents = "role: ${pillar.role}\n"; }
      ];
    }

A Nix attribute set has no order, so Salt sees its states sorted by
name. To keep the order you wrote, use a list of sets with one state
each:

    #!nix
    [
      { first."test.nop" = [ ]; }
      { second."test.nop" = [ ]; }
    ]

A function must name its arguments. `x: ...` is refused.

A Nix error is a Salt render error. Its position reads `«string»:line:column`,
and the line is the line in the SLS file.

## Files beside the SLS

A relative path names a file on Salt's fileserver, in the SLS's
saltenv:

    #!nix
    let ports = import ./ports.nix; in
    { web."test.nop" = [ { port = ports.http; } ]; }

The evaluator reads the saltenv through the render's file client: in a
state render that serves file_roots, and in a pillar SLS pillar_roots.
It lists the files once per render, and fetches a file the first time
Nix reads it. The tree appears in Nix at a store path named
`salt-<saltenv>`, which no store holds.

## Pillar from a Nix file

On the master:

    ext_pillar:
      - nix: /srv/nix-pillar/default.nix

The file is an attribute set, or a function that takes
`{ id, grains, pillar, ... }`:

    { id, grains, pillar, ... }:
    let roles = import ./roles.nix; in
    {
      role = roles.${id} or "default";
      ntp.servers = if grains.os == "Leap" then [ "0.opensuse.pool.ntp.org" ] else [ ];
    }

`pillar` holds what the earlier pillar sources gave. A relative path
names a file beside this one, on the master's disk. The dictionary form
also takes `store`:

    ext_pillar:
      - nix:
          file: /srv/nix-pillar/default.nix
          store: dummy://

## Evaluating on a minion

    salt '*' havsorm.eval 'builtins.currentSystem'
    salt '*' havsorm.eval '{ grains, ... }: grains.os'

The module is `havsorm`, because Salt's own `nix` module drives the
`nix-env` command.

## Configuration

    havsorm:
      store: dummy://

`store` is the Nix store the evaluator uses. The default, `dummy://`,
holds nothing, so a file that reads a derivation or copies a path
fails. Set `auto` on a minion that has Nix.

## Installing

    salt-pip install havsorm

Salt finds the renderer through the `salt.loader` entry point. This
needs huggorm on PyPI, which is not published yet. Until then, build
with Nix:

    nix build --file . havsorm

## Uyuni and SUSE Multi-Linux Manager

A client renders its states in the Salt bundle, `venv-salt-minion`:
Salt 3006.0 on Python 3.11. Install havsorm into it the way the bundle
documents for any package:

    salt <minion> pip.install havsorm

A formula or a state channel can then hold `#!nix` SLS files and the
`.nix` files they import. `nix build --file . bundle` runs the bundle's
own `salt-call` on such files.

## Limits

- Each render makes a new evaluator, and collects after it.

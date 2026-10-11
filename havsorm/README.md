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

The evaluator reads the saltenv through the minion's file client. It
lists the files once per render, and fetches a file the first time Nix
reads it. The tree appears in Nix at a store path named
`salt-<saltenv>`, which no store holds.

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

## Limits

- A pillar SLS reads its relative paths from the file_roots tree, not
  from pillar_roots.
- Each render makes a new evaluator.

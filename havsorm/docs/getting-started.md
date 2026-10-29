# Getting started

`packages.saltEnv` is a Python with Salt and havsorm. Its `bin/` holds
`salt-call`, `salt-minion` and the other Salt commands.

## Build Salt with havsorm

Put this in `env.nix`:

```nix
{ pkgs ? import <nixpkgs> { } }:
(import (builtins.fetchTarball "https://github.com/nixidae/havsorm/archive/main.tar.gz") {
  inherit pkgs;
}).packages.saltEnv
```

The first build compiles huggorm and the Nix it links.

## Apply a state written in Nix

A masterless minion needs one configuration file. Put this in
`conf/minion`:

```yaml
file_client: local
root_dir: .
file_roots:
  base:
    - srv
```

Put the state in `srv/hello.sls`. The function gets the minion's grains:

```nix
#!nix
{ grains, ... }:
{
  greeting."test.succeed_without_changes" = [
    { name = "hello from ${grains.kernel}"; }
  ];
}
```

Apply it:

```console
$ nix build --file env.nix
$ ./result/bin/salt-call --local --config-dir conf state.apply hello
local:
----------
          ID: greeting
    Function: test.succeed_without_changes
        Name: hello from Linux
      Result: True
```

[How to use](usage.md) shows the rest: pillar from a Nix file, calls to
Salt from Nix, reactors, and the states for the Nix store and NixOS
hosts.

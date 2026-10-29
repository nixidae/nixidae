# Getting started

A test is a set of guests and a set of phases. A guest is a NixOS module.
A phase is a Python file with one coroutine. `lib.mkTest` makes the test.

## Write a test

Put this in `test.nix`. Pass your own `pkgs`, so that the guests use your
package set:

```nix
{ pkgs ? import <nixpkgs> { } }:
let
  inherit ((import (builtins.fetchTarball "https://github.com/nixidae/vivarium/archive/main.tar.gz") {
    inherit pkgs;
  }).lib) mkTest;
in
mkTest {
  name = "hello";
  nodes.machine = {
    environment.systemPackages = [ pkgs.hello ];
  };
  phases.check = {
    script = ./check.py;
    after = [ "boot" ];
  };
}
```

`boot` is a phase that every test has. It waits until each guest is up.

Put the phase in `check.py`:

```python
from vivarium_runner import Machines


async def test(vms: Machines) -> None:
    await vms.machine.succeed("hello")
```

## Run it

```console
$ nix build --file test.nix --print-build-logs        # in the sandbox
$ nix run --file test.nix driver -- --out ./out        # by hand, output in ./out
```

The build fails when the test fails. A run by hand writes its log, one
file for each guest and `events.jsonl` to `./out`.

The [howto](howto.md) tells you how to pause a run, look inside the guests
and change a phase without a new boot. [Test options](test-options.md)
lists what `mkTest` takes, and [guest options](guest-options.md) lists the
`vivarium` options of a guest.

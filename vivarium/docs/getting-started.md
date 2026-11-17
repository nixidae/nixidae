# Getting started

A test is a set of guests and a set of phases. A guest is a NixOS module.
A phase is a Python file with one coroutine. `lib.mkTest` makes the test.

## Write a test

Put this in `test.nix`:

```nix
let
  inherit ((import (builtins.fetchTarball "https://github.com/nixidae/vivarium/archive/main.tar.gz") { }).lib) mkTest;
in
mkTest {
  name = "hello";
  nodes.machine =
    { pkgs, ... }:
    {
      environment.systemPackages = [ pkgs.hello ];
    };
  phases.check = {
    script = ./check.py;
    after = [ "boot" ];
  };
}
```

`boot` is a phase that every test has. It waits until each guest is up.

With no arguments, vivarium uses the nixpkgs that nixidae pins, so its
store paths agree with nixidae's. To build the guests from your own
package set, pass it: `import (...) { pkgs = import <nixpkgs> { }; }`.
Then the first run also builds the User-Mode Linux kernel from that set,
which takes minutes.

vivarium finds its other sources with `builtins.fetchTree`. Outside a
flake, Nix needs the `fetch-tree` feature for it:
`--extra-experimental-features fetch-tree`, or `flakes` in `nix.conf`.

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
and change a phase without a new boot. `nix build --file . docs.default`,
in a checkout of vivarium, builds these docs with two more pages: test
options, what `mkTest` takes, and guest options, the `vivarium` options of
a guest.

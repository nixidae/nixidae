# Getting started

kappy is a Python library. `lib.overlay` adds it to a pyproject package set,
and `lib.projectRoot` tells the set where kappy declares its dependencies.

## Build an environment

Put this in `env.nix`. nanopynix's set resolves kr8s to the version that
kappy tests against:

```nix
{ pkgs ? import <nixpkgs> { } }:
let
  fetch = repo: builtins.fetchTarball "https://github.com/nixidae/${repo}/archive/main.tar.gz";
  nanopynix = import (fetch "nanopynix") { inherit pkgs; };
  kappy = (import (fetch "kappy") { inherit pkgs; }).lib;
  pythonSet = nanopynix.lib.pythonSetWith {
    projectRoots = [ kappy.projectRoot ];
    inherit (kappy) overlay;
  };
in
pythonSet.mkVirtualEnv "kappy-env" { kappy = [ ]; }
```

Add your own project to `projectRoots` and to the overlay, and kappy
resolves in the same set as your dependencies.

## Apply a generation

kappy has no labels and no field manager of its own. You name them in each
call. Put this in `apply.py`:

```python
import anyio
import kr8s.asyncio
from kappy.apply import apply_and_prune

OBJECTS = [
    {
        "apiVersion": "v1",
        "kind": "ConfigMap",
        "metadata": {"name": "hello", "namespace": "default"},
        "data": {"greeting": "hello"},
    }
]


async def main() -> None:
    await apply_and_prune(
        OBJECTS,
        api=await kr8s.asyncio.api(),
        environment="dev",
        environment_label="example.com/environment",
        unit_label="example.com/unit",
        field_manager="example",
    )


anyio.run(main)
```

```console
$ nix build --file env.nix
$ ./result/bin/python apply.py
```

The call applies the ConfigMap to the cluster that your kubeconfig selects,
and labels it `example.com/environment=dev`. A later call with the same
environment deletes each labelled object that it does not apply.

`kappy.testing.FakeApi` stands in for the cluster in a test:
`api=FakeApi().as_api()`.

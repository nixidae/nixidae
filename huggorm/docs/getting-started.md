# Getting started

`packages.huggorm` is a Python package for the interpreter of the `pkgs`
you pass. Put it in a Python environment.

## Build an environment

Put this in `env.nix`:

```nix
{ pkgs ? import <nixpkgs> { } }:
let
  huggorm = import (builtins.fetchTarball "https://github.com/nixidae/huggorm/archive/main.tar.gz") {
    inherit pkgs;
  };
in
pkgs.python3.withPackages (_: [ huggorm.packages.huggorm ])
```

huggorm compiles against the Nix that its own lock names. The first build
compiles that Nix too.

## Add a path to a store

Put this in `hello.py`. `dummy://` is a store in memory, so it needs no
daemon. It is read-only unless you say otherwise:

```python
import huggorm

store = huggorm.Store("dummy://?read-only=false")
path = store.add_to_store("hello", b"hello\n")
print(store.print_store_path(path))
```

```console
$ nix build --file env.nix
$ ./result/bin/python hello.py
/nix/store/...-hello
```

`huggorm.Store("auto")` opens the store that your Nix configuration names,
usually the daemon. The [quickstart](quickstart.md) shows the rest of the
API: path information, evaluation, the async classes and workers.

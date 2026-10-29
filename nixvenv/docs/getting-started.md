# Getting started

nixvenv is one function, `lib.nixvenv`. It takes an interpreter and a
package list, as `python.withPackages` does, and returns a venv.

## Build a venv

Put this in `venv.nix`. Pass your own `pkgs`, so that the venv uses your
package set:

```nix
{ pkgs ? import <nixpkgs> { } }:
let
  inherit ((import (builtins.fetchTarball "https://github.com/nixidae/nixvenv/archive/main.tar.gz") {
    inherit pkgs;
  }).lib) nixvenv;
in
nixvenv {
  python = pkgs.python3;
  packages = ps: [ ps.httpx ];
}
```

Build it, and run its interpreter:

```console
$ nix build --file venv.nix
$ ./result/bin/python -c 'import sys, httpx; print(sys.prefix)'
/nix/store/...-python3-3.14.7-venv/lib/venv
```

## Choose the executables

`executables` selects which programs of the venv appear in `result/bin`.
The default is the interpreter alone. This venv also gives `pytest`:

```nix
nixvenv {
  python = pkgs.python3;
  packages = ps: [ ps.pytest ];
  executables = [ "python" "pytest" ];
}
```

`editable` adds source directories that the venv imports in place. The
[doc comment of `nixvenv.nix`](https://github.com/nixidae/nixvenv/blob/main/nixvenv.nix)
describes each argument.

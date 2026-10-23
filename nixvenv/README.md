# nixvenv

A real Python venv from nixpkgs packages. `sys.prefix` and `sys.executable`
are the venv, a subprocess of `sys.executable` sees the same packages, and
no PYTHONPATH is set. Every tool that asks Python where its packages are
gets the same answer as the interpreter.

```nix
let
  inherit (import ./nixvenv { inherit pkgs; }) nixvenv;
in
nixvenv {
  python = pkgs.python3;
  packages = ps: [ ps.httpx ps.pytest ];
  executables = { python = null; pytest = null; };
}
```

`nixvenv.nix` documents the arguments. `checks.nix` holds what a venv
promises, measured from inside it.

Developed in [nixidae](https://github.com/nixidae/nixidae); this repository
is an export of its `nixvenv/` directory.

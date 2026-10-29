# nixvenv

A real Python venv from nixpkgs packages. `sys.prefix` and `sys.executable`
are the venv, a subprocess of `sys.executable` sees the same packages, and
no `PYTHONPATH` is set. Every tool that asks Python where its packages are
gets the same answer as the interpreter.

```{toctree}
:maxdepth: 2
:caption: Contents

getting-started
```

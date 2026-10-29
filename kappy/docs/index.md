# kappy

The apply engine of easykubenix, as a library. kappy applies a set of
Kubernetes objects with server-side apply, then prunes the objects that the
previous generation applied and this one does not. It uses kr8s, and it does
not need Nix.

```{toctree}
:maxdepth: 2
:caption: Contents

getting-started
usage
```

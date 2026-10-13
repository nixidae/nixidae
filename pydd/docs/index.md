# pydd

Dynamic derivations, planned in Python. A planner is a derivation that
decides at build time what Nix builds next. pydd plans Python
environments from `uv.lock` and `poetry.lock`, with one derivation per
package, on Nix master and on released Nix.

The [README](https://github.com/Lillecarl/pydd#readme) says how to use
it. These pages hold what the work found.

```{toctree}
:maxdepth: 2
:caption: Findings

modes
```

```{toctree}
:maxdepth: 1
:caption: Research notes of the ddrn lab

README
ARCHITECTURE
NIX-FACTS
PACKAGING-FACTS
UPSTREAM
```

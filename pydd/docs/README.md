# Research notes

These notes come from the ddrn lab of nanopynix, on the branch
`ddrn-lab-hetztop` of Lillecarl/nanopynix. pydd's Python planner
(`src/pydd/python/`, `lib/python/`) is a port of that lab.

| file | holds |
| --- | --- |
| `ARCHITECTURE.md` | the shape of the planner, in phases |
| `PACKAGING-FACTS.md` | each measured fact about Python packaging and lock files |
| `NIX-FACTS.md` | each measured fact about dynamic derivations in Nix |
| `UPSTREAM.md` | the three changes to Nix that the lab made |

**The notes name the lab's paths**, such as `examples/venv-graph/plan.py`.
In pydd, that file is `src/pydd/python/_environment.py`.

**pydd needs none of the three changes in `UPSTREAM.md`.** It runs on
stock Nix master:

- **EnsurePath:** huggorm attaches the string context, in
  `pydd.modes.evaluate._document.document_value`, so `graph.nix` calls neither
  `builtins.storePath` nor `builtins.appendContext`. The `cli` writer runs
  no evaluator: `pydd.modes.render` writes the same derivations.
- **The name of a submitted derivation:** each planner is named after the
  root that it submits.
- **`nix eval --submit`:** the writer of the planner submits the root.

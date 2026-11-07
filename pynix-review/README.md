# pynix-review

What a nixpkgs change rebuilds, in what order, and how long Hydra took to
build it. A library and a CLI for review bots, built on
[huggorm](https://github.com/nixidae/huggorm).

pynix-review evaluates the same attributes in two nixpkgs trees. Each tree
gets its own evaluator over an in-memory store, so no `.drv` file reaches
disk. It diffs the two derivation closures and layers the changed set by
depth, two ways:

- longest path: depth 1 has no changed input, and building depth k or less
  builds exactly what it selects;
- nearest: the distance from what the change touched, which is what
  "direct dependents" means. A depth here can need more builds than it
  selects.

A fixed-output derivation matches by its content address, so a fetch whose
`.drv` moved but whose output did not counts as unchanged.

pynix-review is part of [nixidae](https://github.com/nixidae/nixidae). Work
happens there.

# pydd

Dynamic derivations, planned in Python.

A planner is a derivation that decides at build time what Nix builds next.
It writes a graph of derivations through the restricted store socket of a
`builder-rpc-v0` build, and submits the root. `builtins.outputOf` on the
planner builds the graph.

The motivating case is Python packaging. `uv2nix` and `pyproject.nix` carry a
copy of PEP 508 markers, PEP 440 specifiers and PEP 425 tags written in Nix.
`packaging` is the reference implementation of all three, and a planner runs
it unchanged.

## Requirements

- Nix master, for `builder-rpc-v0` (NixOS/nix#15793). It is in no release.
- `experimental-features = nix-command ca-derivations dynamic-derivations`.

## Three ways to write a graph

| | writes through | needs | example |
| --- | --- | --- | --- |
| `pydd.mkPlanner`, `writer = "cli"` | `nix derivation add`, `nix store submit-output` | Python stdlib, a `nix` | `examples/hello` |
| `pydd.mkPlanner`, `writer = "huggorm"` | `Store.add_derivation`, `Store.submit_output` | huggorm | `examples/hello` |
| `pydd.evaluate.mkEvaluated` | the Nix evaluator, in the planner | huggorm | `examples/evaluated` |

No mode renders ATerm. A node is derivation JSON, and Nix computes every
output path it can. For the same plan, the CLI and huggorm writers submit
the same root `.drv`.

`pydd.python.mkEnvironment` builds a Python environment from a lock file
(`examples/venv`). Each wheel, sdist and editable project is a node. The PEP
517 backend of an sdist comes from the same lock and is a node too, so the
graph cannot exist before the plan runs. Only the artefacts the target
needs are downloaded: 6 of 21 in the example.

## Constraints

- A node the planner writes has a floating output, or one Nix computes.
  Another node names it by `pydd.downstream_placeholder`.
- A planner writes its graph leaves first: Nix checks each input when it
  writes a node.
- The planner is named `<root>.drv`. Nix requires a submitted object to
  have the name of the planner's output, and the root is `<root>.drv`.
- In a Nix expression, `builtins.storePath` and `builtins.appendContext`
  call `EnsurePath`, which `builder-rpc-v0` refuses. `mkEvaluated` attaches
  context from Python instead, so the expression needs neither.

## Running the examples

This machine's daemon may be older than Nix master. `run` builds in a
private store with a Nix from master, and copies the planner's closure in:

    nix run --file . run -- examples.cli.hello
    nix run --file . run -- examples.huggorm.venv check
    nix run --file . run -- examples.huggorm.evaluated

`PYDD_STORE` names the store; the default is `~/.cache/pydd/store`.

## Tests

    nix build --file . checks.tests

The suite holds pydd's JSON and placeholders to what Nix's own
`derivation` makes, and the resolver to the example lock file.

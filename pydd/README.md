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
  On a release, a planner can write only one derivation, to `$out`, as
  ATerm. pydd has no mode for that yet: it waits for huggorm#163.
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

`pydd.python.mkEnvironment` builds a Python environment from a `uv.lock`
(`examples/venv`). Nix only parses the lock and makes one fetch per file.
The planner walks its dependency edges, with markers and extras. Each
wheel, sdist and editable project is a node. The PEP 517 backend of an
sdist comes from the same lock and is a node too, so the graph cannot exist
before the plan runs. Only the artefacts the target needs are downloaded:
9 of the 122 in the example.

uv locks neither build backends nor the installer. A dependency group puts
them in the lock, and an sdist names its backend through `buildSystems`.
Registry packages and workspace members work. Git, URL and path sources do
not yet.

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

## A tour

Read the examples in this order. Each folder has a README with the
command to run and what it prints.

1. `examples/hello`: the smallest planner, two nodes in Python.
2. `examples/evaluated`: the same idea, with the graph as a Nix file.
3. `examples/venv`: a Python environment from a `uv.lock`.

An example is `examples.<name>.<writer>`:

    nix run --file . run -- examples.hello.cli
    nix run --file . run -- examples.venv.huggorm check

Your daemon may be older than Nix master. `run` builds in a private store
with a Nix from master, and copies the planner's closure in. `PYDD_STORE`
names the store; the default is `~/.cache/pydd/store`.

## Layout

| path | holds |
| --- | --- |
| `lib/` | the Nix side: `mkPlanner`, `mkEnvironment`, `mkEvaluated` |
| `src/pydd/` | the Python side: `Derivation`, `Planner`, the writers |
| `src/pydd/python/` | the `uv.lock` reader, the resolver and the builders of a Python environment |
| `examples/` | the tour |
| `tests/`, `nix/` | the test suite, and the umbrella pins |

## Tests

    nix build --file . checks.tests

The suite holds pydd's JSON and placeholders to what Nix's own
`derivation` makes, and the resolver to the `uv.lock` of the example.

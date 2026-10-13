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
  On a release, a planner writes only one derivation, as ATerm, to `$out`.
  `mkPlanner { protocol = "out"; }` does that (`examples/released`), and
  the host makes every other node. Python environments use it with
  `plannerProtocol = "out"` (see Python environments).
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

## Python environments

`pydd.python.uv { project = ./.; }` builds the environment of a uv project
(`examples/venv`). Nix only parses `uv.lock` and makes one fetch per file.
The planner reads the lock the way uv installs it, and chooses an artefact
for each package with `packaging`. Only the chosen artefacts are
downloaded.

**The writer chooses one of two paths from plan to derivations.** Both
paths make the same derivations:

| writer | the plan becomes derivations through | needs |
| --- | --- | --- |
| `huggorm` | `lib/python/graph.nix`, evaluated in the planner | huggorm |
| `cli` | `pydd.python._render`, in Python | `packaging`, a `nix` |

`graph.nix` is the reference. `checks.tests` holds the renderer to it for
each node kind, and `test-projects` builds each project both ways and
compares the two environments.

**On released Nix an environment keeps one node per package.** A planner
there writes only its root, so the host makes the rest:

- the install node of every wheel in the lock, from `graph.nix`;
- a planner for each sdist, as on `builder-rpc-v0`;
- a planner for each local project, which submits that project's node
  (two for a project built from its own sdist);
- a planner for the environment under an application.

`graph.nix` reads a node that another planner writes, of kind `planned`,
through `builtins.outputOf`. All 14 test projects build on stock Nix 2.35
this way, with the same environment paths as on `builder-rpc-v0`.

**An sdist gets a planner of its own.** Its build backend is declared
inside the archive, so no evaluation can know it. That planner reads the
archive, chooses the backend from the same lock, and submits the build.

The result also carries `env`, `app` and `shell`. The reader handles
workspaces, git, URL and path sources, and local registries.
`tests/projects` builds one project for each (see Tests). uv locks neither
build backends nor the installer, so a dependency group puts them in the
lock.

`pydd.python.poetry { project = ./.; }` does the same for a Poetry
project, at lock version 2.1 or later (`examples/poetry`). Only the reader
of the lock file differs. `poetry.lock` records no URL, so this reader
takes PyPI packages only.

This planner is a port of the ddrn lab of nanopynix. `docs/` holds its
research notes.

## Extending a build

pydd is a framework. A package that needs more than its lock file says
gets it through one of these extension points, and each one works in
every mode, released Nix included.

| need | extension point | status |
| --- | --- | --- |
| native inputs of a source build | none: nixpkgs hints (`nixpkgsHints`, `lib/python/hints.nix`) | done, #3 |
| a library linked through another one | none: the fixup step (`scripts/fixup-elf.py`) | done, #4 |
| tools that nixpkgs does not imply | `[tool.pydd.build-tools]`, or `buildTools` | done |
| a package from nixpkgs instead of PyPI | `[tool.pydd.from-nixpkgs]`, or `fromNixpkgs` | done |
| env, `config_settings`, patches, hooks | node overrides | #5 |
| a build that nothing above covers | a host function `artefact -> derivation` | #6 |

**The hints follow your nixpkgs.** They come from `python.pkgs`, so an
overlay or `packageOverrides` that changes a nixpkgs Python package
changes what pydd builds that package with. A declaration in
`[tool.pydd.build-tools]` or `buildTools` replaces the hints for that
package.

**The fixup step needs no declaration.** A node that builds with tools
adds to each ELF file's `RUNPATH` the directory of every `DT_NEEDED`
entry that it does not resolve. It searches the tools' `lib` directories,
then the `RUNPATH` of each library it finds there. So pyzmq finds
libsodium through libzmq. A file that already resolves keeps its bytes.

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
3. `examples/nested`: a planner whose graph reads another planner's graph.
4. `examples/venv`: a Python environment from a `uv.lock`.
5. `examples/poetry`: the same environment from a `poetry.lock`.
6. `examples/released`: `hello` on a released Nix, with no `builder-rpc-v0`.

An example is `examples.<name>.<writer>`:

    nix run --file . run -- examples.hello.cli
    nix run --file . run -- examples.venv.huggorm check
    nix run --file . run -- examples.venv.cli check

Your daemon may be older than Nix master. `run` builds in a private store
with a Nix from master, and copies the planner's closure in. `PYDD_STORE`
names the store; the default is `~/.cache/pydd/store`.

## Layout

| path | holds |
| --- | --- |
| `lib/` | the Nix side: `mkPlanner`, `mkEvaluated` |
| `lib/python/` | `pydd.python.uv`, `pydd.python.poetry`, and `graph.nix`, which turns a plan into derivations |
| `src/pydd/` | the Python side: `Derivation`, `Planner`, the writers |
| `src/pydd/python/` | the planners of a Python environment: lock readers, selection, plan, renderer |
| `examples/` | the tour |
| `docs/` | the research notes of the ddrn lab |
| `tests/`, `nix/` | the test suite, and the umbrella pins |

## Tests

    nix build --file . checks.tests
    nix run --file . test-projects

`test-projects` builds the uv projects of `tests/projects`, which cover
workspaces, git, URL and path sources, a local registry, binary and native
wheels, applications, and nixpkgs packages in place of PyPI ones. Each one
needs `builder-rpc-v0` or a release, so it builds in the private store of
`run` or `run-released`. It builds each project in three modes, the two
writers on `builder-rpc-v0` and `out` on Nix 2.35, and fails when the
environments have different store paths.

The suite holds pydd's JSON and placeholders to what Nix's own
`derivation` makes. `tests/python` holds the Python planner to the lock
files that uv wrote.

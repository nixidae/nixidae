# The modes, compared

pydd builds one Python environment in four modes. The plan is the same in
each mode. A mode decides how the plan becomes derivations, and how the
derivations reach the store.

**The modes give the same environment, and a test proves it.**
`nix run --file . test-projects` builds each of the 19 test projects in
every mode. It fails when two modes give different store paths for one
environment. So a mode differs from another in cost and in what it needs,
and never in what it builds.

## What the names mean

**A mode is two choices, and the four names mix them.** One choice is the
Nix that the planner runs under. The other is the tool that writes the
derivations.

### The Nix: `builder-rpc-v0`, or `$out`

**`builder-rpc-v0` is on Nix master, and in no release.** Nix gives the
planner a restricted daemon socket. The planner writes each derivation of
its graph through that socket, and then submits the root as its output.
`cli`, `huggorm` and `drv` run this way.

**On a release, a planner writes one derivation: its root.** It writes the
root as ATerm text to `$out`, and Nix reads that text as a derivation. The
planner cannot add any other derivation, so the host makes every other
node before the planner runs. `out` runs this way, on Nix 2.35.

### The writer: `nix`, huggorm, or `pydd-drv`

- **`cli` is the stock `nix` binary.** The planner runs
  `nix derivation add` once for each derivation, and
  `nix store submit-output` for the root.
- **`huggorm` is Python bindings to Nix's own C++ libraries**, libnixstore
  and libnixexpr. It is not a second implementation of Nix: the code that
  runs is Nix's. The planner writes in process, and it can evaluate Nix
  code: this mode evaluates `lib/python/graph.nix`.
- **`drv` is `pydd-drv`**, a small C++ tool over libnixstore
  (`tools/pydd-drv`, Lillecarl/pydd#1). It is the specialised CLI.
  `add` and `submit` do what the two `nix` commands do, and `graph`
  writes a whole graph of JSON derivations in one process.
- **`out` writes through huggorm** today. huggorm stages each derivation
  in an in-memory store, and the planner copies the root's ATerm to
  `$out`. `pydd-drv graph --out` does the same for a planner in any
  language (`examples/graph`). pydd's own planners cannot use it yet,
  because they add one derivation per call, and a new process does not
  keep the in-memory store (Lillecarl/pydd#15).

### The four modes

| mode | Nix | writer | plan to derivations | the host evaluates |
| --- | --- | --- | --- | --- |
| `cli` | master, `builder-rpc-v0` | `nix` | `pydd.modes.render`, in Python | one fetch per artefact, and a planner per sdist |
| `huggorm` | master, `builder-rpc-v0` | huggorm | `lib/python/graph.nix`, in huggorm's evaluator | the same |
| `drv` | master, `builder-rpc-v0` | `pydd-drv` | `pydd.modes.render`, in Python | the same |
| `out` | 2.35, `$out` | huggorm | `pydd.modes.render`, in Python | also a node per wheel, a planner per local project, and the environment's planner under an application |

### How `out` builds a graph from one derivation

**The host is the evaluation that the user starts**, such as
`nix build --file . env`. It runs before any build, outside every
builder, the way the evaluation of a nixpkgs package does. It
*instantiates* derivations: it writes each `.drv` to the store, and
builds nothing. A planner is a builder, which runs later, in a sandbox.

`lib/modes/out.nix` makes the host's nodes. It evaluates `graph.nix` for
each one, takes its `.drv` path with `unsafeDiscardOutputDependency`, and
passes the list to every planner as `hostNodes`. Each `.drv` is then an
input of the planner, present in its sandbox, while no output of it is
built.

**A planner on a release writes one derivation, and that derivation may
name others that already exist.** A root names its inputs by `.drv`
path. Nix accepts any input that is in the store, and the planner's
sandbox holds every input of the planner. So the host makes each `.drv`
that a root could need before the planner runs, and the planner decides
which of them the root names. Three things make that work:

1. **The host makes a node for every wheel of the lock**, for every
   platform, with the same `graph.nix` and the same document that the
   planner would write. So the host's `.drv` and the planner's are one
   path. The host only instantiates them: Nix builds the ones that the
   root names, and no other one.
2. **A node that only a build can shape gets a planner of its own.** The
   backend of an sdist is declared inside the archive, and no evaluation
   has read the archive. The host makes one planner per sdist, one per
   local project, and one for the environment under an application. Each
   writes one derivation, its node, as its own `$out`. The root names
   that node as a dynamic output (`builtins.outputOf`), a node of kind
   `planned`. So one node per package holds: each package's derivation
   is either one the host made, or the one derivation of its own
   planner.
3. **The planner computes every path exactly, and writes only the
   root.** huggorm stages each derivation in an in-memory store, so the
   paths match the other modes byte for byte. The planner then copies the
   root's ATerm to `$out`. Nix registers no reference to a path that the
   sandbox lacks, and the root then silently becomes another derivation.
   So the writer refuses a root that names such a path.

**So `out` chooses at build time, and it does not construct.** Its
planners run in builders, and make each decision that needs a build:
which wheel fits the target, and what an sdist's archive declares. Every
node they name is one the host made before. That suffices when the host
can list the candidates, as one node per package can. It fails when the
number or the shape of the nodes is known only after a build, such as a
node per source file of an archive, or nesting deeper than the host
prepared (`sdistPlannerDepth`). `builder-rpc-v0` constructs: a planner
writes any derivation it decides on.

**The cost is on the host.** It instantiates a node for every wheel it
might need, and a planner wherever only a build can decide. With
`builder-rpc-v0` a planner writes what it chose, and nothing more.

**"Plan to derivations" is a third, smaller choice.** `graph.nix` says how
a node is built. `pydd.modes.render` makes the same derivations in Python,
for a planner with no evaluator. `tests/test_render.py` holds the two to
the same `.drv` for every node kind.

## Measured

```{include} _generated/measurements.md
```

`tools/measure-modes.py` measures this, after `test-projects` built every
node. It adds a nonce to each planner of an environment, so Nix runs the
planner again, and every node the planner names is already built. So a
time is the planner's own. The planner of an sdist, and the host-made
planners of `out`, do not run again.

**At this scale the mode does not decide the time.** A build that does
nothing takes 1.3 to 1.4 s, so a planner's own work is 1 to 2 s, and
the four modes are within a second of each other. The test projects lock
at most a few packages. The ddrn lab measured lock files of up to 1911
packages (`PACKAGING-FACTS.md`), and a large lock is where the modes can
differ: one process per node in `cli` and `drv`, one evaluator in
`huggorm`, and host evaluation of every wheel in `out`. That measurement
is still to do.

- **Planner closure** is what one planner needs in its sandbox: an
  interpreter, pydd, and the tool that writes.
- **Lines of the mode** count the files that only that mode needs, beside
  the core and the Python layer that every mode shares.

## What each mode cannot do

**`cli` and `drv` run no evaluator in the planner.** So `graph.nix` exists
twice: in Nix, and as `pydd.modes.render` in Python. `tests/test_render.py`
holds the two to the same `.drv` for every node kind.

**`huggorm` cannot call `builtins.storePath` or `builtins.appendContext`.**
Both call `EnsurePath`, and `builder-rpc-v0` refuses it. huggorm attaches
the string context from Python instead
(`pydd.modes.evaluate._document.document_value`). A plain `nix eval` in
the planner has no such way.

**No `builder-rpc-v0` mode can evaluate nixpkgs in a planner.** Measured in
Lillecarl/pydd#9: a planner that imports nixpkgs fails after 33 s with
`Operation 26 not allowed inside derivation`. Operation 26 is
`QueryPathInfo`. `fetchToStore` calls it after it copies a path literal
into the store, and nixpkgs has a path literal in every setup hook.
Lillecarl/nix#371 allows `EnsurePath`, and not `QueryPathInfo`.

**`out` makes the host do the planning that a planner cannot.**

- The host makes a node for every wheel of the lock, for every platform,
  because only the planner knows which wheel it chooses.
- An sdist, and a local project, become planners that the host made. A
  planner reads the output of another through `builtins.outputOf`.
- A root that names a path the sandbox does not hold silently becomes
  another derivation. `OutWriter` and `pydd-drv graph --out` refuse such a
  root.

## What this asks of Nix

Each item is a change to Nix, and each one is measured above or in the
issue it names.

1. **`EnsurePath` in `builder-rpc-v0`** (Lillecarl/nix#371). A planner can
   then run a plain `nix eval` over `graph.nix`, with no huggorm. pydd#8
   is the mode that would show it.
2. **`QueryPathInfo` in `builder-rpc-v0`, or a `fetchToStore` that does
   not query.** A planner can then evaluate nixpkgs and write nodes with
   `stdenv.mkDerivation` (pydd#9). Without it, only the host can use
   stdenv, as `builders` does (pydd#6).
3. **Tools over the daemon protocol, and not a second protocol.**
   `pydd-drv` (pydd#1) is the shape: `add` and `submit` are the two
   commands Nix has, and `graph` writes a whole graph in one process. It
   computes each placeholder and writes ATerm to `$out` on a release.
   Its closure is not much smaller than `nix`'s, because libnixstore is
   most of both. A subcommand of Nix would cost nothing extra.
4. **Nothing else.** One derivation per package works on released Nix
   today, through host-made nodes. The cost is host evaluation, and the
   table above shows what the planners cost.

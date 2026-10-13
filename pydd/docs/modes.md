# The modes, compared

pydd builds one Python environment in four modes. The plan is the same in
each mode. A mode decides how the plan becomes derivations, and how the
derivations reach the store.

**The modes give the same environment, and a test proves it.**
`nix run --file . test-projects` builds each of the 19 test projects in
every mode. It fails when two modes give different store paths for one
environment. So a mode differs from another in cost and in what it needs,
and never in what it builds.

## The four modes

| mode | Nix | plan to derivations | derivations to the store | the host evaluates |
| --- | --- | --- | --- | --- |
| `cli` | master | `pydd.modes.render`, in Python | `nix derivation add`, `nix store submit-output` | one fetch per artefact, and a planner per sdist |
| `huggorm` | master | `lib/python/graph.nix`, in huggorm's evaluator | `Store.add_derivation`, `Store.submit_output` | the same |
| `drv` | master | `pydd.modes.render`, in Python | `pydd-drv add`, `pydd-drv submit` | the same |
| `out` | 2.35 | `pydd.modes.render`, in Python | the root as ATerm, in `$out` | also a node per wheel, a planner per local project, and the environment's planner under an application |

The first three modes use `builder-rpc-v0`: a planner writes derivations
through a restricted daemon socket, and submits the root. Nix master has
it, and no release has it. On a release a planner writes one derivation,
its root, so the host makes every other node before the planner runs.

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

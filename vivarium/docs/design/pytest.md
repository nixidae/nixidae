# pytest as the front door (proposal, 2026-11-17)

User asked whether `pytest` in a dev shell should drive vivarium: a
plugin that evaluates and boots guests through huggorm, with guests as
fixtures, scenarios on a class, and forked "hot" guests for speed. This
note holds what was measured and checked, the options, and a
recommendation. Nothing here is built yet.

## What exists

- `vivarium.pytest_plugin`: pytest as a *phase*. vivarium owns the run,
  Nix orders the phases, and pytest runs in a worker thread against
  guests that are already up. Each guest is a fixture by name. Async
  tests and fixtures go back to the session's loop through a
  `BlockingPortal`. This is "vivarium outside, pytest inside".
- `Session` boots a step at a time: `boot(only)`, `ensure(names)`,
  `launch(template, name)` and `terminate(name)` (the cloud API, #1408),
  `switch`, `reload`, `teardown`.
- `vivarium-eval` (`vivarium` CLI): huggorm evaluates `--file`, builds
  `.driver`, runs it.
- A guest reads the host's store. So a dev shell's Python, and the test
  code in a shared directory, run inside a guest unchanged.

## Measured

| step | time |
| --- | --- |
| evaluate `checks.forward.qemu.spec` (one guest), fresh process | 3.6 s |
| evaluate `checks.lan.qemu.spec` (two guests) | 4.2 s |
| evaluate `checks.k8s.qemu.spec` (three guests) | 5.2 s |
| boot one NixOS guest until its agent answers, UML, QEMU or krun | 7.6-8.2 s |

A cold `pytest -k one_test` against one fresh guest costs about 12 s
before the test body runs. The design is mostly about where those 12 s
go.

## The two directions

**vivarium outside, pytest inside** (today). Nix declares guests and
phases; a failure skips what depends on it; a sandboxed `nix build` is
the verdict. pytest is one kind of phase.

**pytest outside, vivarium inside** (proposed). `pytest` in a dev shell
is the entry point. A plugin finds what the selected tests ask for,
evaluates and builds it in one batch, boots guests on demand, and hands
them out as fixtures.

What the second buys: the tool people know. `-k`, `--lf`, `--pdb`, IDE
test discovery and "run this one test", xdist, every reporter (JUnit,
pytest-agent), parametrize over machine types. What it costs: a second
front door to keep true, and pytest has no "a failure skips what
depends on it" except through fixtures.

**Recommendation: both, one mechanism.** Keep `mkTest` as the
Nix-ordered contract that CI builds. Add the plugin as the development
front door. Both use the same `Session`, the same `Machine` API and the
same portal code, so a test file runs unchanged either way.

## The plugin's shape

### Machines are Nix, chosen by name from Python

Guests stay NixOS modules in a Nix file. Python only names them:

```python
pytestmark = pytest.mark.vivarium(file="tests/machines.nix")

def test_hostname(vm: Machine):              # `vm` = machines.default
    ...

@pytest.mark.vivarium(machine="big")         # machines.big
async def test_build(vm: Machine): ...

@pytest.mark.vivarium(scenario="k8s")        # scenarios.k8s: nodes, segments
class TestCluster:
    async def test_nodes(self, vms: Machines): ...
```

Why not define guests in Python: the sandbox cannot evaluate. With
machines in Nix, a Nix function can build every machine a suite names
ahead of time, and the same suite runs in `nix build`:

```nix
vivarium.lib.pytestCheck { tests = ./tests; machines = ./tests/machines.nix; }
```

In the sandbox the plugin reads prebuilt specs from the environment and
evaluates nothing. In a dev shell it evaluates through huggorm.

### Lifetimes are pytest scopes

| scope | means |
| --- | --- |
| function | a fresh guest per test: isolated, and slow unless pooled or forked |
| class | a scenario for a class: the "cluster scenario" |
| module, session | one guest shared: fast, and each test leaves state for the next |

A `reset` between tests is a second lever: `switch` back to the
machine's own system, or, on QEMU, a snapshot restore.

### Only what the selected tests need

`pytest_collection_modifyitems` (trylast, after `-k` deselects) sees the
selected items and their marks. The plugin then evaluates those machines
in one huggorm evaluation and builds them in one realise. Booting starts
for the first class while pytest still sets up. xdist: one `Session` per
worker; `--dist loadscope` keeps a class's scenario on one worker.

### Where the test body runs

- **On the host** (default): the test drives guests through `Machine`.
- **In a guest** ("containment"): the test function itself runs inside
  a guest, as root, with the guest's kernel. The guest has the dev
  shell's interpreter (shared store) and the project directory (a
  share). execnet, which xdist uses, starts a Python over any stdio
  pipe, so a worker gateway can run through the guest agent. Use:
  tests that need root, namespaces, a kernel option or a disposable
  system. pynixd's root suite is that case today ("never the suites with
  host pytest"), and it now needs a Nix-built guest run to do it.

### Evidence and pausing

One out directory per pytest session: `events.jsonl`, consoles, the
control socket. `--vivarium-keep` (or a failed test under `--pdb`) keeps
the guests up, and `vivarium ctl`, the MCP server and `exec` reach in,
as with a paused run today.

## Hot guests

Two ways to hide the 8 s boot, in order of cost:

1. **A warm pool** (every backend). Keep N booted spares per machine,
   from the cloud API's `launch`. A test takes one, and the plugin
   terminates it after the test and boots a replacement in the
   background. Simple, and it uses code that exists. A test still waits
   when the pool is empty, and each spare costs its memory.
2. **Forked guests** (QEMU only). Boot a template once, then start each
   clone from its memory, copy-on-write. QEMU 11 here has what it takes:
   - migration capabilities `x-ignore-shared` and `mapped-ram`;
   - `memory-backend-file` with `share=off`, which maps the template's
     RAM copy-on-write;
   - virtiofsd 1.14 migrates its state (`--migration-mode find-paths`).

   Kata Containers' VM templating works this way. Each clone needs its
   own virtiofsd and egress. The vsock uplink helps here: every vsock
   guest is 10.0.2.15 behind its own egress, so a clone needs no new
   address. Clones share the template's random state; reseed it after
   the restore. Expected start: well under a second. Not measured.

krun has no snapshot API. UML has no checkpoint; CRIU on a UML process
is not tried.

## Risks

- Evaluation at every `pytest` start: 3.6-5.2 s. A cache keyed by the
  machine file's inputs helps; huggorm's long-lived evaluation service
  (#553) removes it.
- Two front doors drift. Shared `Session` and fixtures keep the gap
  small; a check runs one suite both ways.
- A shared guest hides order dependencies between tests. A function
  scope (pooled or forked) is the honest default once it is fast.

## Order of work

User chose (2026-11-17): both front doors, host-run tests first, and
the plugin as a *client* of a run in its own process, not a `Session`
inside pytest. Inside, every test would be root of vivarium's user
namespace, xdist workers would need care, and guests would die with
each `pytest`.

1. Done: `vivarium_eval.pytest_plugin`, a pytest11 entry point.
   `vivarium up` starts a run per scenario and scope; the run serves
   each guest's methods on `machines.sock` (`vivarium.remote`, arpyc);
   tests get proxies. `--vivarium-keep` reuses runs across invocations.
   `examples/pytest-host`, QEMU, two scenarios and four tests:

   | run | time |
   | --- | --- |
   | cold, machines built | 37.8 s |
   | `--vivarium-keep`, first | 36.8 s |
   | `--vivarium-keep`, second | 0.46 s (tests 0.07 s) |

   The cold run is about 19 s a scenario: `vivarium up` (evaluate,
   build check, boot) and `vivarium down`.

## Direction after slice 1 (User, 2026-11-17)

User sees the plugin as pytest-xdist with isolation: vivarium supplies a
deterministic userspace and a box per test, and pytest stays as
expressive per test as it is. Exploratory; take ideas from pytest and
its plugins.

**Two ways a test meets a guest.**

- *It drives guests* (slice 1): the test runs on the host, and the
  guests are fixtures.
- *It runs in a guest*: pytest-forked's pattern, with a guest where
  pytest-forked has a forked process. A marker sends the item to a
  worker in a guest. The worker runs the item's whole protocol there
  (fixtures, the test, teardown) and sends pytest's own serialised
  reports back (`pytest_report_to_serializable`, as xdist does). The
  host relays them from `pytest_runtest_protocol`, so `-k`, `--lf`,
  `-x` and every reporter see an ordinary test. The guest reads the
  host's store, and the project directory is shared at the same
  absolute path: the worker is the dev shell's own Python, imports
  match, and so do source paths.

**A marker says what box a test needs**, per test, class or module:
which machine, and whether a guest another test used will do (`dirty`)
or it needs one no test touched (`clean`). Marker names are
identifiers, so it is `vivarium(...)` with keywords, not
`vivarium.clean`.

**A pool in the run.** The run keeps spare booted guests from its
templates (the cloud API's `launch` and `terminate`, #1408).
`machines.sock` gains `acquire(template, clean)` and `release(name,
dirty)`. A function-scoped fixture is then an `acquire` and a
`release`, with no boot in the test's time while the pool has a spare.

**A Python-init guest.** For a test that needs a kernel and a clean
userspace but no services: a kernel, the host's store, and Python as
PID 1. It mounts `/proc`, `/sys`, `/dev` and the project share, reaps
children, and serves the worker over the agent channel. No NixOS
system, no systemd. A busybox guest of the same shape booted in 1.0 s on
UML and 1.2-1.5 s on krun (router lab E1-E3), against 7.6-8.2 s for
NixOS. With a pool, a clean box per test is then the cheap default.
Tests that need services (pynixd's daemon, a cluster) keep NixOS
guests.

**Coverage.** coverage.py in the worker, in parallel mode, writes
`.coverage.<suffix>` into the shared project directory. The paths are
the host's, so pytest-cov's combine merges guest and host data with no
path remapping.

**Transport: arpyc throughout.** It already carries the agent channel
and `machines.sock`; one protocol, one round trip a call. In a NixOS
guest the agent starts the worker; in a Python-init guest the worker is
PID 1 and speaks on the channel the agent would. execnet is not needed:
the store and the project share replace its code shipping.

## Order of work

1. Done: drive guests from host tests (above).
2. Done: `lib.pytestCheck`. The plugin moved to the runner's package
   (`vivarium.pytest_host`, no huggorm), so the sandbox's Python needs
   no nanopynix. Under `VIVARIUM_RUNS` it starts each run from a built
   `.up` and waits for `ready` with the runner's own `watch`; it stops a
   run with the control socket's `stop` in every mode.
   `checks.pytest-host`: 4 passed in 22.4 s in the sandbox.
3. Done: a test runs in a guest with `vivarium(attr,
   guest="vm")`. The tests of one scope share one pytest in that guest
   (`vivarium.pytest_worker`), kept alive: it collects the scope's files
   once, then `pytest_runtest_protocol` asks it for one test at a time,
   with the next test's id so a fixture lasts as long as in one process,
   and relays that test's reports. The host reaches the worker's arpyc
   socket through the agent (`Machine.relay`): 0.3-0.5 ms a call, against
   30-80 ms for a command plus 60-90 ms to start Python. `-x` stops the
   guest too, since the host asks for no more tests; `stop` at the end of
   the scope tears down and saves coverage. `checks.pytest-worker` holds
   `-x`, the teardown, a passing test's output and a collection error
   that happens in the guest alone. The guest gets this Python through
   `Machine.add_paths` (dev: `vivarium closure`; sandbox: `pytestCheck`'s
   closureInfo) and the project as a tarball through `/artifacts`,
   unpacked at its own path. Sync and async tests both run there.
   Coverage: with pytest-cov active, the worker measures its pytest with
   coverage.py, as pytest-cov's options say (`Measure`, read from the
   options, not pytest-cov's objects). The host copies the data file
   beside pytest-cov's own as a parallel file, and pytest-cov's combine
   at the end merges it; paths match, so nothing is remapped.
   `checks.pytest-coverage` reaches 100% of `sample` only through the
   guest; with the worker measuring nothing it reads 50%.
4. The Python-init guest. Measure boot to a ready worker.
5. The pool, the `clean`/`dirty` marker keyword, function-scoped boxes.
6. QEMU forked guests, if 4 and 5 leave a start time worth cutting.

Remembered for later (User, 2026-11-17):

- **Grouping across modules.** `scope` groups by class, module or
  session today. A `group="name"` keyword, as xdist's `xdist_group`,
  would put tests from several modules in one guest.
- **Parametrize over machines.** One test against several variants, such
  as guests with different Nix daemons or clients: the marker takes a
  list of attributes, and `pytest_generate_tests` makes one test each.
- **Sync host tests.** A host test that drives guests is `async def`
  today. Sync proxies, each call through the plugin's portal, serve
  people who do not write async.

## Shortcuts to circle back to

What was taken to get a slice working, and where it should go:

- **The project is copied, not shared.** A tarball of the whole rootdir
  (minus `.vivarium`, `.git`, caches, `result*`) per guest, every time a
  run is prepared. Fine for a small project; a large one wants a
  read-only virtiofs or hostfs share at the same path, or an
  incremental copy. The copy does keep the guest from writing into the
  working tree.
- **Output pytest does not capture** (`-s`, or a fixture's thread) goes
  to the worker's log, `/artifacts/pytest/worker-*.log`, not to the
  host's terminal. The host shows the log only when the worker fails.
- **The guest collects whole files.** A node id the guest's pytest
  cannot collect is a usage error that ends it, so the worker gets the
  scope's files and collects every test in them, selected or not.
- **A worker the host never stopped stays up** in the guest until the
  run ends: a host pytest killed mid-scope leaves it. Each has its own
  socket, so a kept run's next session starts a new one.
- **The worker's Python has no Nix registration** in the guest
  (`add_paths`, not `add_closure`): fine for running Python, wrong for a
  test that asks the guest's Nix about those paths.
- **Dev mode asks the `vivarium` CLI for the closure**, a subprocess per
  prepared guest.
- **One timeout for each test**, 3600 s, its fixtures included.
- **Host-side proxies carry plain data only**, and an async fixture of
  the user's own cannot use `vms` yet.
- **`--vivarium-keep` does not notice** that the machines changed.
- **Coverage follows pytest-cov's central mode only.** Under xdist
  (`DistMaster`) the guest's file still lands beside the data file, but
  that path is not tested. `--cov-context` is not passed to the guest.

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

## Order of work, if wanted

1. The plugin with host-run tests, function and class scopes, machines
   from a Nix file, evaluated through huggorm. Measure the cold run.
2. `pytestCheck`: the same suite sandboxed with prebuilt specs.
3. The warm pool.
4. Tests in a guest, through execnet. Try it on pynixd's root suite.
5. QEMU forked guests: a prototype that measures clone start.

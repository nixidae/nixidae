# Architecture

These pages tell how vivarium works inside: which code does each step,
and in which order. They are for a person who changes vivarium, and for
a review of it. The [how-to guide](../howto.md) and the
[reference](../reference.md) tell how to use it.

Each statement names the file that does the work. Each box in a graph
names its file and its definition in the graph's source,
`docs/architecture/*.dot`. `checks.docs` fails when a named file or
definition is gone, so a graph cannot point at code that does not exist.
The check does not prove that an edge is still true: read the code when
the two disagree, and correct the page.

```{toctree}
:maxdepth: 1

evaluation
run
guests
```

These pages do not cover yet:

- the events and the evidence: `vivarium/events.py`, `sinks.py` and
  `journal.py`, and `vivarium_runner/report.py`;
- pytest phases (`vivarium/pytest_plugin.py`, `pytest_worker.py`) and
  pytest outside a run (`pytest_host.py`, `remote.py`);
- named environments and kept disks (`kept.py`, `environments.py`);
- the cloud API that guests call (`vivarium/cloud.py`);
- recipes (`modules/recipes/`) and Kubernetes (`modules/k8s.nix`,
  `pkgs/vivarium-k8s`, `pkgs/vivarium-lb`);
- guest kinds other than NixOS, and console guests;
- the boot inside a guest: `/init`, the store overlay and the agent's
  unit.

## Three times

A test goes through three times. Each one decides some facts and passes
them on.

1. **Evaluation.** Nix evaluates `mkTest` (`lib.nix`). This decides the
   guests, their indexes, MAC addresses and IP addresses, whether the run
   has a router, and the order of the phases. The result is one JSON
   file, the spec.
2. **Build.** Nix builds everything the spec names: each guest's root
   image and system, the guest's closure, the runner's Python
   environment, and the type check of every phase script.
3. **Run.** The program `vivarium-runner run --spec SPEC` reads the spec,
   boots the guests, runs the phases and writes the evidence. It never
   evaluates Nix.

The run does not evaluate, so a sandboxed check needs no evaluator and
no Nix daemon. A command that must evaluate during a run, such as
`vivarium switch` or `vivarium reload`, evaluates and builds in the
`vivarium` CLI. It then sends the built store path to the run over the
run's control socket ([The run](run.md)).

## The parts

```{graphviz} packages.dot
```

| Package | Programs | Role |
| --- | --- | --- |
| `lib.nix`, `modules/` | none | `mkTest`, the guest modules, the spec |
| `pkgs/vivarium-eval` (`vivarium_eval`) | `vivarium`, `vivarium-mcp` | The commands a person or an agent runs. They evaluate through huggorm. |
| `pkgs/vivarium` (`vivarium`) | `vivarium-runner` | The driver: one `Session` per run, the phase order, the control socket |
| `pkgs/vivarium-runner` (`vivarium_runner`) | none | The mechanism: `Run`, `Machine`, the backends, the segments, the forwards |
| `pkgs/vivarium-agent` (`vivarium_agent`) | `vivarium-agent` | The agent in a NixOS guest, and the arpyc protocol both ends speak |
| `pkgs/vivarium-egress` | `egress`, `relay` | The uplink: the relay in the guest or in the router, the egress on the host |

The driver owns the order of the steps. The mechanism has no opinion
about the order: it boots, drives and stops guests when it is told to.
Pure rules, such as "which phases are ready", are functions in
`vivarium/phases.py` and have tests that boot no guest.

The Python package `vivarium` holds the program `vivarium-runner`, and
the package `vivarium_runner` holds no program. The `vivarium` command
comes from `vivarium_eval`. Read a name in these pages as the package
name unless it says "program".

## How a run starts

Every way to start a run ends in the same program, `vivarium-runner
run`. Only the flags differ.

```{graphviz} entry.dot
```

- **`nix build <test>`** builds `<test>.attempt`, which runs the program
  with `--no-control` and `--random-ports` in the build sandbox. The
  attempt never fails, so Nix keeps its output. The test derivation reads
  the attempt's `status` file and fails or passes on it (`readVerdict`).
- **`nix run <test>.driver`** runs a wrapper that `wrap` in `lib.nix`
  makes: the program with `--spec` and the variant's flags baked in.
  `.driverDebug` adds `--break-on-failure`, `.driverInteractive` adds
  `--interactive` and `.up` adds `--hold`.
- **`vivarium run <attr>`** evaluates the file with huggorm, builds the
  attribute's `.driver` and replaces itself with that program (`os.execv`
  in `vivarium_eval/cli.py`). It does not call its own copy of the
  runner: the program it builds comes from the same `lib.nix` as the
  spec, so it knows every field of the spec.
- **`vivarium up <attr>`** and `vivarium run --until-pause` start
  `vivarium run` as a child in a session of its own; `up` adds `--hold`
  (`child_args`). The parent follows the child's events and returns at
  `ready` or at the first pause, with the guests still up.
- **MCP `start`** starts `vivarium run <attr>` as a child process, or the
  spec's own `vivarium-runner` when it is given a spec (`run_argv` in
  `vivarium_eval/server.py`).

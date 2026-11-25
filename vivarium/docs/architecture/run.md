# The run: from `vivarium-runner run` to the exit status

`vivarium-runner run --spec SPEC --out OUT` is one process. It reads the
spec, boots the guests, runs the phases, writes the evidence to `--out`
and exits with a status from `vivarium/exits.py`. This page follows that
process. [Guests](guests.md) tells how one guest boots and how the
guests reach each other.

```{graphviz} run.dot
```

## Before the event loop

`main` in `vivarium/cli.py` does three things before it starts anyio:

1. **The namespace.** `namespace.enter` runs the program again with
   `unshare`, as root of a new user namespace with a private mount
   namespace. A guest's store view is a set of bind mounts, and a bind
   mount needs that. `--offline` also gives the run a network namespace
   that holds only `lo`, which is what a build sandbox gives.
2. **The run root.** Before the re-exec, `namespace.enter` makes the run
   root, a directory for everything the run writes outside `--out`, and
   points `TMPDIR` at it. `runroot.spawn_cleaner` forks a cleaner in a
   session of its own. The cleaner waits on the runner's pidfd and
   removes the root when the runner exits, also after SIGKILL.
   `runroot.reap` removes the roots of earlier runs whose runner and
   cleaner are both gone.
3. **The lock on `--out`.** `liveness.hold` locks `--out`, so a second
   run into the same directory fails at once. The `vivarium` CLI takes
   the same lock before it evaluates and passes it down
   (`liveness.hand_down`).

## The task tree

`run` reads the spec (`Spec.read`), makes the `Session` and marks the
phases that `--only` leaves out as `deselected`. Then it starts one task
group. Everything after this lives in a task of that group or of a group
under it, so nothing outlives the run.

- `_stop_on_signal` turns SIGTERM and SIGHUP into the same path as ^C.
  `_stop_with_parent` does the same when the process that started the
  run exits. Both cancel one scope, `sequence`, and nothing else.
- `drive` in `vivarium/driver.py` holds a second task group. The tests
  call it without the command line:
  - `Session.follow` copies each guest's journal into the events, for
    the whole run, a pause included.
  - With a control socket, `Controller.serve` listens on `control.sock`
    and `remote.serve` on the socket that `vivarium.pytest_host` uses.
    Both sockets exist before the first guest boots.
  - With `--interactive`, `repl.serve` attaches a REPL.
- `drive` writes `run.json`, then enters `Session.live`, a third task
  group for the run's background tasks, such as a cloud instance's boot.
  `_sequence` runs inside it.

## The sequence

`_sequence` in `vivarium/driver.py` is the order of a run:

1. **Boot.** `Session.boot` prepares the run (`Session.prepare`) and
   boots the guests. A full run boots every guest first, because a phase
   may reach a peer that it names nowhere. Under `--only`, each phase
   boots its own guests when it starts (`Session.ensure`).
2. **Pause at start**, with `--break start` or `--interactive`.
3. **The phases**, in `_schedule` (below).
4. **Pause at the end**, with `--break end` or `--interactive`.
5. **Hold**, with `--hold`. When every phase passed, the run pauses at
   `ready`. A `continue` there runs what a `reload` added, then holds
   again. Only `ctl stop` ends the hold.

The `finally` block of `_sequence` runs in a shielded cancel scope, so a
^C or a signal still writes the evidence and stops the guests:

1. `Session.drain`: the guests' last journal lines.
2. `Session.write_output`: `status`, `phases.json` and `report.json`,
   then the `run_finished` event.
3. `Session.teardown`: each guest powers off. A run with no control
   socket kills them, because nobody can reach them any more.
4. `Session.write_report`: `report.json` again, with the time of the
   teardown.
5. `access.empty`: the run removes its access files, because a
   kubeconfig there names guests that are gone.

`run` then returns 0 when every phase passed or was deselected
(`phases.passed`), and 1 otherwise. A stopped run returns the status of
its stop.

## The phases

`_schedule` starts each phase as soon as it may start. It does not run
in waves: a wave waits for its slowest phase. Each time a phase ends, it
asks the pure functions in `vivarium/phases.py` again:

- `ready`: the pending phases whose every `after` has an answer. A
  failure marks its dependents `skipped` first, so a phase that is still
  pending after a failure is an `always` phase.
- `launchable`: the ready phases that hold no guest that a running phase
  holds. A phase holds the guests in its `nodes`, or every guest when it
  has none. A pytest phase always holds every guest, because
  `pytest.main` is not reentrant in one process.

`--serial` starts one phase at a time. A breakpoint or a failure under
`--break-on-failure` stops new phases, and the pause begins when the
running phases have ended. So a paused run has no phase running, and a
phase that a person runs by hand never races a scheduled one.

`Session.run` runs one phase. It boots the phase's guests
(`Session.ensure`), makes the phase's own `vms` (`Session.view`), and
calls the script's `test(vms)` or runs pytest. It then sets the phase's
state, emits `phase_finished` and, on a failure, marks the dependents
`skipped` (`skipped_by`).

```{graphviz} phase-states.dot
```

| State | Meaning | The run passes |
| --- | --- | --- |
| `passed` | the phase ended with no error | yes |
| `unneeded` | the phase found its work done already (`vms.skip`) | yes |
| `deselected` | `--only` left the phase out | yes |
| `failed` | the phase raised an exception | no |
| `skipped` | a phase in its `after` failed | no |
| `interrupted` | the run stopped while the phase ran | no |
| `pending` | the phase never started | no |

An `always` phase is not skipped by a failure before it, and it stops
the skip from reaching the phases after it (`dependents`).

## The control socket

`Controller` in `vivarium/control.py` serves `control.sock`, a Unix
socket with mode 0600 in the run's runtime directory
(`vivarium/places.py`). A request is one JSON line with an `Op`, and
the reply ends with one JSON line. A request that asks for a stream
(`exec`, `inject` and `pytest` from `vivarium ctl`) gets its output in
lines before the reply. `vivarium ctl`, `vivarium monitor` and the MCP
server are its clients.

- `state`, `exec`, `inject`, `stop`, `segment`, `switch` and `watch`
  work at any time (`Controller.ANY_TIME`). `exec` runs Python in the session's namespace, where `vms`,
  `session` and each guest are names.
- `pytest`, `run`, `reload` and `continue` work only while the run is
  paused.
- `watch` streams the run's channel events (`vivarium/feed.py`) until
  the run ends. `vivarium monitor` reads it.
- `stop` cancels `sequence` after the reply, as a signal does. At
  `ready`, it ends the hold and the run exits with its verdict.

A pause is `Controller.pause`: it emits `paused` (or `ready`) and the
command lines that reach the run, then waits for `continue`.

`reload` and `switch` carry a store path that the `vivarium` CLI
evaluated and built. `Session.reload` takes the new phases, settings and
knobs, and refuses changed guests, templates or segments. `switch` adds a closure to
one guest (`Machine.add_closure`), switches it (`Machine.switch_to`) and
changes its forwards to the new `vivarium.inbound`
(`Machine.switch_inbound`).

A sandboxed check runs with `--no-control`: it has no control socket,
no remote socket and no pause.

---
name: vivarium
description: Run and debug vivarium tests (nixidae's NixOS test runner on UML, QEMU, krun and container guests) -- choose sandboxed or by-hand runs, wait with `vivarium monitor`, pause on failure and reach into live guests with `vivarium ctl` or the vivarium MCP tools, read events.jsonl. Load before running any vivarium `mkTest`/`fromNixosTest` attribute, a `.driver`, or a test suite that runs in vivarium guests (pynixd, nixkube, easykubenix and others).
---

# vivarium

A test is `mkTest`: guests (`nodes.<name>`, NixOS modules) plus phases (`async def test(vms)` files or pytest), ordered by `after`. Every test attribute has run variants; the guests are the same in each.

## Choose the run

| attribute | when |
| --- | --- |
| `nix build --file . <test>` | the verdict, as CI runs it. No network, no way in. A failure keeps its output: `nix eval --raw --file . <test>.attempt` |
| `nix run --file . <test>.driverDebug -- --out DIR` | anything that may fail: pauses at the first failure, guests stay up |
| `nix run --file . <test>.driver -- --out DIR` | by hand, exits at the first failure. Add `--break-on-failure` for the same pause as `driverDebug` |
| `<test>.uml` `.qemu` `.krun` `.container` | the same test on one backend |

- A test that pulls from the internet must run as a driver. In the sandbox it fails on DNS, which reads like a broken test.
- `--offline` makes a by-hand run match the sandbox. A sandbox failure reproduces this way; there is no way into a sandboxed run.
- Driver flags: `--break PHASE|start|failure|end`, `--only PHASE` (runs its `after` phases too, nothing after it), `-v` (every command sent), `--keep`, `--cpus [GUEST=]N` / `--cpu-scale [GUEST=]S` (processors of one guest, or of every guest not named; QEMU and krun default to all host cores; MCP `start` takes `cpus`/`cpu_scale` as a number or `{guest: n}`).
- `--out` may be relative; use a new directory per run, in the scratchpad. Files a container guest wrote there can be owned by a sub-uid and resist `rm` (nixidae/nixidae#1064): pick a new name instead.
- Never edit a phase and start a new run to test the edit. Pause and send it, or `vivarium reload` (below).

## Wait

One command, in the background, run bare: it starts the run detached (its output in `$OUT/run.log`) and exits at the first pause or at the verdict, with the monitor's codes. The guests stay up after it exits 4.

```sh
vivarium run <test> --out "$OUT" --break-on-failure --until-pause    # background
vivarium ctl --out "$OUT" continue --wait                            # after probing: next pause or verdict
```

Without the `vivarium` CLI, start the driver and the monitor together, both in the background:

```sh
nix run --file . <test>.driverDebug -- --out "$OUT" > "$OUT.log" 2>&1   # background
vivarium monitor "$OUT" --quiet                                          # background
```

- The monitor waits up to `--wait` (1800 s) for the run to start, because the driver evaluates and builds first. No `until [ -e ... ]` loop, no `sleep`. For a run `vivarium run` started, it prints each derivation as it starts building (not with `--quiet`), and exits 2 with the error if evaluating or building fails; `$OUT/build.jsonl` holds every Nix log record, build log lines included.
- Exit: 0 passed, 1 failed, 2 no verdict (crash, eval error), 3 stream lost or no run started, 4 paused, 5 stopped. Re-arm after each `continue`, or use `ctl continue --wait`: it exits at the next pause or the verdict with the same codes.
- `--through-pauses` runs to the verdict; without `--quiet` it also prints each phase start and pass.
- With a `nix run` driver, the driver's own exit is the other half: a build or eval failure ends the driver and the monitor then times out. Read `$OUT.log` when the driver task ends first.
- A background task exiting 0 is not a pass. Read the monitor's exit code, `status`, or `phases.json`.
- Run the monitor bare. `vivarium monitor ... | tail` or `...; echo rc=$?` makes the task exit 0 at a pause; a paused run then reads as "completed (exit code 0)".
- Name what you wait on when you stop: "waiting on the monitor for <test> (task X); then <next step>".
- Never wait on GitHub Actions. A local pass is the verdict.

## At a pause

`vivarium` is on PATH in the dev shell of each project that runs vivarium tests (`packages.vivarium`, built on huggorm). The run also prints absolute command lines at start and at each pause, and `$OUT/run.json` holds them, with the test's name, guests, phases and spec. A run the `vivarium` CLI started prints `vivarium` lines; a `nix run` driver prints its runner's path. Never scrape `$OUT.log` for them.

```sh
vivarium ctl --out "$OUT" state
vivarium ctl --out "$OUT" exec 'print(await one.succeed("systemctl --failed"))'
vivarium ctl --out "$OUT" exec - < probe.py
vivarium ctl --out "$OUT" inject ./tests/check.py        # a file's async def test(vms), read fresh
vivarium ctl --out "$OUT" pytest ./tests/guest -- -k etcd
vivarium ctl --out "$OUT" run check                       # a declared phase
vivarium ctl --out "$OUT" continue
vivarium ctl --out "$OUT" continue --wait                # then wait: exit 4 at the next pause, else the verdict
vivarium ctl --out "$OUT" watch                          # progress, pauses, verdict: one line each, to the end
vivarium ctl --out "$OUT" stop                           # end it here, guests down
```

- `exec`: Python on the host, top-level await; `vms`, `session`, `anyio` and each guest by name in scope; names persist between calls. `exec` and `inject` print the code's output as it comes, and `pytest` one line per case as it ends (`--json`: the whole reply at the end). `exec`, `inject`, `state` and `stop` also work while phases run; `pytest`, `run`, `continue` need a pause.
- Loop: read the failure, probe with `exec`, edit the test file, `inject`/`pytest` again, repeat until it passes, then `continue`.
- `inject` reads the phase file fresh, but a helper module on the test's `pythonPath` stays the store copy the run imported. To test an edited helper, name its working-tree directory: `vivarium ctl --out "$OUT" inject ./phases/x.py --reload ./lib` (repeatable; `pytest` takes it too). The directory goes first on `sys.path`, and the modules it provides are dropped.
- A run started by `vivarium run <attr>` also prints `vivarium reload --out O`: evaluate the test again and give the paused run its edited phases (scripts, pytest specs, `after`, `nodes`, settings), guests kept. Then `ctl run <phase>` or `continue`. Refused if a guest changed. A phase that ran keeps its verdict until run again.
- A run started by `vivarium run <attr>` also prints a `switch` line (`vivarium switch --out O --node N --module ./module.nix`; the attribute comes from `run.json`): edit a NixOS module, switch one guest to it, no new run. A `nix run` driver prints none.
- `ctl stop` ends the run, paused or running: guests down, exit 5, verdict `stopped`. It returns once the run has exited. Never kill the driver by pid or pattern.
- `ctl state` prints `paused before <phase>` (or `running`), then `phase\tstate`, with the first line of the error on a failed phase. `ctl --json <op>` prints the whole reply as one JSON object: `pause`, `state`, `errors` (full text).
- Do not pipe `ctl` into `tail`/`head`: a refusal goes to stderr and exit 1, and the pipe hides both.
- Guest helpers: `succeed`, `execute` (rc, out), `fail`, `wait_for_unit`, `journal(unit)`, `processes()`, `meminfo()`, `switch_to(name)`, screen methods on QEMU guests with `vivarium.display`.

## MCP

The `vivarium` server (`vivarium-mcp`) does the same through tools: `start(attr, file, env, breaks, break_on_failure, only)`, `state`, `exec`, `inject`, `run_pytest`, `run_phase`, `resume`, `switch`, `reload`, `screenshot`, `events`, `runs`, `stop`.

- `start` takes `attr` (with `file`, the directory or file to evaluate) or `spec`, never `file` alone. It runs with `--break-on-failure`.
- `start` returns `monitor`: run it with the Monitor tool or in the background, exactly as above.
- The `vivarium-runs` mod (`share/claude-plugins/vivarium-runs`, if loaded) shows each run in a pane with Continue and Stop; it follows `start` and `vivarium run --out <literal>` by itself, `/vivarium-runs <out>` any other.
- `events` replies have no size limit: always pass `kind`, `machine`, `phase` or `contains`, and a small `limit`. One unfiltered `case` query returned 233k characters.

## Evidence

In `--out` (by hand) or `result/` / `.attempt` (sandboxed):

| file | holds |
| --- | --- |
| `run.json` | written before the first guest boots: `vivarium` (the CLI), `socket`, `pid`, `commands` |
| `events.jsonl` | every event, with `kind`, `machine`, `phase`, `seconds`. Query with `jq`, not grep |
| `phases.json` | each phase's state and why one was skipped |
| `console/<guest>.log` | one guest's console |
| `log` | everything, readable |
| `report.json` | where the time went: `.slowest`, `.by_command` |
| `artifacts/<guest>/` | what the guest wrote to `/artifacts`, including `journal.jsonl` |

```sh
jq -r 'select(.kind=="phase_finished") | "\(.phase)\t\(.data.state)"' "$OUT/events.jsonl"
jq -c 'select(.kind=="journal" and .data.priority <= 3)' "$OUT/events.jsonl"
jq -c 'select(.kind=="case" and .data.outcome=="failed") | .text' "$OUT/events.jsonl"
```

## Traps

- `vivarium.memory` below ~192M OOM-kills the guest agent; it looks like a hang.
- A guest agent failure surfaces as an exception from `vm.execute`; the traceback is in `await vm.journal("vivarium-agent")`.
- Writes to `/nix/store` in a guest vanish at poweroff, by design.
- A run cleans up however it ends, SIGKILL included. A guest process (QEMU, libkrun, UML) left after the run ended is a vivarium bug worth an issue.
- The working copy reaches a run only through Nix: in a jj repository a new file needs a jj command first.

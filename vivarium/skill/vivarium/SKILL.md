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
| `nix run --file . <test>.driver -- --out DIR` | by hand: runs every phase that does not depend on a failed one, then exits 1. Add `--break-on-failure` for the same pause as `driverDebug` |
| `<test>.uml` `.qemu` `.krun` `.container` | the same test on one backend |

- A test that pulls from the internet must run as a driver. In the sandbox it fails on DNS, which reads like a broken test.
- `--offline` makes a by-hand run match the sandbox. A sandbox failure reproduces this way; there is no way into a sandboxed run.
- Driver flags: `--break PHASE|start|failure|end`, `--only PHASE` (runs its `after` phases too, nothing after it), `-v` (every command sent), `-q` (failures and the verdict only), `--keep`, `--serial` (one phase at a time), `--hold` (an environment: guests up after the last phase), `--interactive` (a REPL before the first phase), `--kernel [GUEST=]PATH` (your own kernel, in one guest or every guest not named; MCP `start` takes `kernel` as a path or `{guest: path}`), `--cpus [GUEST=]N` / `--cpu-scale [GUEST=]S` (processors of one guest, or of every guest not named; QEMU and krun default to all host cores; MCP `start` takes `cpus`/`cpu_scale` as a number or `{guest: n}`).
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
- Exit: 0 passed, 1 failed, 2 no verdict, 3 no run, 4 paused, 5 stopped, 6 ready, 7 refused (`docs/howto.md` says what each means). Re-arm after each `continue`, or use `ctl continue --wait`: it exits at the next pause or the verdict with the same codes.
- `--through-pauses` runs to the verdict; without `--quiet` it also prints each phase start and pass.
- With a `nix run` driver, the driver's own exit is the other half: a build or eval failure ends the driver and the monitor then times out. Read `$OUT.log` when the driver task ends first.
- A background task exiting 0 is not a pass. Read the monitor's exit code, `status`, or `phases.json`.
- Run the monitor bare. `vivarium monitor ... | tail` or `...; echo rc=$?` makes the task exit 0 at a pause; a paused run then reads as "completed (exit code 0)".
- Name what you wait on when you stop: "waiting on the monitor for <test> (task X); then <next step>".
- Never wait on GitHub Actions. A local pass is the verdict.

## Environments

Guests that stay up after the phases, for work from the host (a cluster to `kubectl` against):

```sh
vivarium up <test> [--name N] [--daemonize]   # background; exits 0 at ready with access.json on stdout, 4 if a phase failed (paused); --daemonize: own systemd user unit vivarium-N.service, outlives the caller's cgroup
vivarium shell N [-- CMD]           # $SHELL or CMD with the run's variables: KUBECONFIG, SSL_CERT_FILE, ALL_PROXY...
vivarium env N [--json]             # the same as `export` lines
vivarium logs N [-g GUEST] [-u UNIT] [-k KIND] [--grep T] [-n N] [-f]   # events as `log` shows them; any run, live or not
vivarium ps                         # live runs (state, environment, test, dir), then environments that are down
vivarium down N                     # 0 if every phase passed; the directory stays
vivarium rm N / vivarium clean      # delete one stopped environment / every one
```

- An environment's directory is `<platformdirs state>/vivarium/envs/<name>` (`~/.local/state/...` on Linux); the name is the test's `name` unless `--name`. `up` refuses a name that is up. `--out "$OUT"` instead of a name still works for `up`, `down`, `env` and `shell`.
- `shell`/`env` read `<out>/access/environment.json` (a phase adds with `await vms.export(NAME, VALUE)`). With the router's SOCKS proxy they set `ALL_PROXY`/`HTTPS_PROXY`/`HTTP_PROXY` and put `unproxy` first on PATH: `nix`, `pynix`, `vivarium` skip the proxy by themselves, `unproxy CMD` any other. `--no-proxy` leaves all that out.
- A named environment keeps `state.json` across runs: `vms.state` (a phase's own JSON keys) and `vms.previous` (each phase's state in the last run with a verdict). `vms.skip(reason)` ends a phase as `unneeded`: answered, the run can pass. Empty and unwritten in every other run.

- `up` is `run --hold` detached; the runner's `--hold` (MCP `start(hold=True)`) pauses at `ready` after the last phase passes. `ctl stop` at ready ends the run with its verdict (exit 0), not `stopped`.
- Way in, written before boot: `$OUT/access.json` (each guest's ssh and forwards), `ssh -F $OUT/ssh_config <guest>` (root, `vivarium.rootPassword`; only a guest whose `vivarium.inbound` lists `{ guest = config.vivarium.sshPort; }`), `$OUT/access/` (what phases write via `vms.access`; `bring_up` writes `access/kubeconfig` when the control plane forwards 6443, the `modules/k8s.nix` default).
- A host port reaches only `vivarium.inbound`, fixed at boot; `{ guest = N; }` (no `host`) takes any free port, named in `access.json` and `vm.reachable(N, protocol)`. A fixed `host` another process holds fails the boot; `--random-ports` (MCP `start`'s default, every sandboxed check) binds any free port. `ctl`, `switch`, `reload` work at ready as at a pause; `continue` at ready runs what a reload added and holds again; only `stop` ends it.
- Plain Nix: `nix run --file . <test>.up -- --out DIR`, in the foreground.
- `checks.k8s-host`: a one-node cluster, ready to `up`.

## At a pause

`vivarium` is on PATH in the dev shell of each project that runs vivarium tests (`packages.vivarium`, built on huggorm). The run also prints absolute command lines at start and at each pause, and `$OUT/run.json` holds them, with the test's name, guests, phases and spec. A run the `vivarium` CLI started prints `vivarium` lines; a `nix run` driver prints its runner's path. Never scrape `$OUT.log` for them.

```sh
vivarium ctl --out "$OUT" state
vivarium ctl --out "$OUT" exec 'print((await one.succeed("systemctl --failed")).stdout)'
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
- Do not pipe `ctl` into `tail`/`head`: an error goes to stderr with exit 1 (it ran and failed), 3 (no run) or 7 (refused), and the pipe hides both.
- Guest helpers: `succeed`, `execute`, `fail` (each returns a `CommandResult`: `returncode`, `stdout`, `stderr`), `wait_for_unit`, `wait_until_succeeds`/`wait_until_fails(cmd, timeout)`, `until(what, check, timeout, evidence=)` (async check returns a value, or `None` for not yet), `never(what, check, timeout)`, `vms.until(what, check(vm), timeout, machines=)`: no hand-written retry loops. `system_state(timeout)` (the boot ended: the init's word and what failed; systemd or dinit), `journal(unit)`, `processes()`, `meminfo()`, `switch_to(name)`, screen methods on QEMU guests with `vivarium.display`.
- Links: `await vms.segment("core").cut("leaf1")` / `.restore(...)` drops a member's frames on the host, carrier up; `.capture()` writes `<out>/pcap/core.pcap` (`segments.core.capture = true`: from boot). Only a hub segment (`segments.core.hub = true`, or three members); no `nft` or `tcpdump` in the guest. By hand: `vivarium ctl --out "$OUT" segment core cut leaf1`.

## MCP

The `vivarium` server (`vivarium-mcp`) does the same through tools: `start(attr, file, env, breaks, break_on_failure, only, hold)`, `attach(out)`, `state`, `exec`, `inject`, `run_pytest`, `run_phase`, `resume`, `segment`, `switch`, `reload`, `screenshot`, `events`, `runs`, `stop`.

- `start` takes `attr` (with `file`, the directory or file to evaluate) or `spec`, never `file` alone. It runs with `--break-on-failure`.
- Every tool takes a run's id or its `--out`: a run from `vivarium up`, the pytest plugin or a shell is attached at first use and never stopped by the server. `runs` lists every run up on this host.
- `start` returns `monitor`: run it with the Monitor tool or in the background, exactly as above.
- The `vivarium-runs` mod (`share/claude-plugins/vivarium-runs`, if loaded) shows each run in a pane with Continue and Stop; it follows `start` and `vivarium run --out <literal>` by itself, `/vivarium-runs <out>` any other.
- An `events` reply holds about 40k characters: the newest matches that fit, and `dropped` counts the rest. Pass `kind`, `machine`, `phase` or `contains`, and a small `limit`.
- A long text in a reply or a channel event keeps its head and tail, and names the file that holds all of it (`<out>/mcp/`, `events.jsonl`, `phases.json`).

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

- `vivarium.memory` below ~192M runs the guest out of memory. The agent protects itself from the OOM killer, so a command or a service dies instead.
- A guest agent failure surfaces as an exception from `vm.execute`; the traceback is in `await vm.journal("vivarium-agent")`.
- Writes to `/nix/store` in a guest vanish at poweroff, by design.
- A run cleans up however it ends, SIGKILL included. A guest process (QEMU, libkrun, UML) left after the run ended is a vivarium bug worth an issue.
- The working copy reaches a run only through Nix: in a jj repository a new file needs a jj command first.

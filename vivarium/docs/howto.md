# vivarium howto

Each section is one task, with the commands for it. The examples use a
test named `mine` in `default.nix`. Replace the name with your test's.

[reference.md](reference.md) explains how a run behaves and why. This
file only tells you what to type.

## Run a test

```console
$ nix build --file . mine                          # in the sandbox, as CI runs it
$ nix run --file . mine.driver -- --out ./out      # by hand, output in ./out
$ nix run --file . mine.driverDebug -- --out ./out # by hand, pause at the first failure
```

A run by hand prints the commands that reach it, before the first
phase and again at each pause:

```
[vivarium] wait for a pause or the verdict (exits 4 at a pause): /nix/store/…/bin/vivarium-runner monitor /home/me/out --quiet
[vivarium] run Python against the guests: /nix/store/…/bin/vivarium-runner ctl --out /home/me/out exec 'print(list(vms))'
[vivarium] run a file's `async def test(vms)`: /nix/store/…/bin/vivarium-runner ctl --out /home/me/out inject ./check.py
[vivarium] each phase's state: /nix/store/…/bin/vivarium-runner ctl --out /home/me/out state
[vivarium] resume a paused run: /nix/store/…/bin/vivarium-runner ctl --out /home/me/out continue
```

Copy a line as it is. The paths are absolute, so the command works from
any directory and needs nothing on `PATH`. `<out>/run.json` holds the same
lines. In the rest of this file, `vivarium` is the CLI in
`packages.vivarium`, on `PATH` in the dev shell of each project that runs
vivarium tests. It is built on huggorm and takes `ctl` and `monitor` as
the runner does. The package also holds `vivarium-mcp` and an agent
skill, `share/skills/vivarium/vivarium`.

## Wait for the run without watching it

```console
$ nix run --file . mine.driverDebug -- --out ./out > run.log 2>&1 &
$ vivarium monitor ./out --quiet
```

Start the monitor together with the run. It waits for the run to start,
for up to 30 minutes (`--wait SECONDS`), because the driver first
evaluates and builds the test.

The monitor prints one line for each pause, failure and verdict. Then
it exits:

| Exit | Meaning |
| --- | --- |
| 0 | The run passed. |
| 1 | The run failed. |
| 2 | The run stopped without a verdict, for example it crashed. |
| 3 | The monitor lost the run's stream before a verdict, or no run started within `--wait`. |
| 4 | The run is paused. |

Start it in the background. When it exits, look at the exit code. After
you resume a paused run, start the monitor again. It does not stop again
for a pause that already ended.

With `--through-pauses`, the monitor prints a pause and runs on until
the verdict. Without `--quiet`, it also prints each phase that starts or
passes.

## Look inside a paused run

A paused run keeps its guests up, in the state the failure left.

```console
$ vivarium ctl --out ./out state
$ vivarium ctl --out ./out exec 'await one.succeed("systemctl --failed")'
$ vivarium ctl --out ./out exec - < snippet.py
```

`exec` runs Python with top-level `await`. `vms` holds every guest, and
each guest is also a name of its own. A name you set stays for the next
`exec`. `exec` and `inject` also work while phases run.

## Fix a failing test without a new run

Do not edit a phase and start a new run. That evaluates and boots again.
Change the test file and send it to the guests that are already up:

```console
$ vivarium ctl --out ./out inject ./tests/check.py         # a file's `async def test(vms)`
$ vivarium ctl --out ./out pytest ./tests/guest -- -k etcd # pytest tests
$ vivarium ctl --out ./out run check                       # a phase from the test
```

`inject` and `pytest` read the file each time, so edit and send again.
A helper module the file imports stays the copy the run imported. To
read an edited helper again, name its directory: `--reload ./lib`.

To change the phases themselves, edit the test's Nix or its scripts and
reload a run you started by attribute:

```console
$ vivarium reload --out ./out             # evaluate again; the run takes the new phases
$ vivarium ctl --out ./out run check      # the new `check`, against the same guests
```

`reload` evaluates the session again and builds it, with the phases'
type check. The paused run then takes the new phase list: scripts,
pytest specs, `after`, `nodes`, settings and knobs. A phase that ran
keeps its verdict until it runs again, and a new phase runs when the
run resumes. The run refuses the reload when a guest changed: a guest
needs a new run, or `switch` (below). The run must be paused. Like
`switch`, `reload` reads the attribute from `<out>/run.json`, and the
MCP server has a `reload(run)` tool.

When the test passes, resume:

```console
$ vivarium ctl --out ./out continue
```

`continue` returns when the run is no longer paused. With `--wait`, it
returns at the next pause or at the verdict instead, and exits as
`vivarium monitor` does: 0 passed, 1 failed, 4 paused, 5 stopped, 6
ready (an environment, below).

`vivarium ctl --out ./out watch` prints the run's progress, pauses,
failures and verdict, one line each, until the run ends. Every run with
a control socket serves this stream; `vivarium monitor` reads it too.

`state` says why the run paused and, for each failed phase, the first
line of its error. `vivarium ctl --out ./out --json state` gives the
whole reply as JSON, with each error in full.

To end a run without the phases that are left, stop it:

```console
$ vivarium ctl --out ./out stop
```

`stop` works paused or running. It returns when the run has exited, after
the guests are down. The run exits 5, and its verdict says `stopped`, not
`failed`.

## Switch a guest to another configuration

Declare each configuration on the node. Each one is the node plus a
module:

```nix
nodes.one.vivarium.configurations.two = {
  services.nginx.enable = true;
};
```

A phase switches the running guest, as `nixos-rebuild` does:

```python
await vms.one.switch_to("two")  # switch: profile, then activate
await vms.one.switch_to("two", "test")  # activate only
await vms.one.switch_to()  # back to the booted system
rc, out = await vms.one.switch_to("two", check=False)  # exit 4: a unit failed
```

Host Nix evaluates each configuration. The guest has no nixpkgs. A
reboot does not boot the new system: the guest always boots the system
in its spec.

## Change a running guest's configuration

Write the change as a module file and apply it to a run you started by
attribute:

```console
$ vivarium run checks.switch --out ./out --break switch &
$ vivarium switch --out ./out --node one --module ./more.nix
$ vivarium switch --out ./out --node one --module ./more.nix --action test
```

`switch` reads the attribute and the file from `<out>/run.json`. A run
started this way prints its own `switch` line at each pause, and
`run.json` holds it. Copy it and change `--node` and `--module`. A run
started by `nix run` prints none: only the `vivarium` CLI can evaluate
it again. To evaluate such a run's session anyway, name the attribute:
`vivarium switch checks.switch --file . --out ./out ...`.

The MCP server's `switch` tool does the same: `switch(run, node, module)`.

The host evaluates the session again with the module on the node, and
builds the system. The guest gets the new closure in its store view and
its Nix database, then switches. Edit the file and apply it again to
iterate. Each apply is the booted configuration plus the file, so a
unit you remove from the file is stopped.

The evaluation reads the working tree as it is now. An edit to the
session's own files reaches the guest too.

## Keep guests up as an environment

Any test can be an environment: its phases set it up, and then the
guests stay up until you stop them. A Kubernetes cluster to work
against:

```console
$ vivarium up checks.k8s-host --out ./k8s
$ kubectl --kubeconfig ./k8s/access/kubeconfig get pods -A
$ ssh -F ./k8s/ssh_config cp
$ vivarium ls
ready	k8s-host	/home/me/k8s
$ vivarium down --out ./k8s
```

`up` starts `vivarium run <attr> --hold` in the background, with its
output in `<out>/run.log`. It returns when every phase passed and the
run says `ready`. It exits 0 and prints `<out>/access.json` on stdout. A
phase that fails pauses the run with the guests up, as
`--break-on-failure` does, and `up` exits 4.

The way in is in `--out`, written before any guest boots:

- `access.json`: each guest's ssh address and forwards. The runner picks
  each guest's forward address when the run starts (`127.0.0.2`
  upwards), so read it from here.
- `ssh_config`: `ssh -F <out>/ssh_config <guest>`. The root password is
  `vivarium.rootPassword` (`uml` by default).
- `access/`: what a phase writes for the host, through `vms.access`.
  `vivarium_k8s.bring_up` writes `access/kubeconfig` there when the
  control plane forwards 6443, which `modules/k8s.nix` does by default.
  The kubeconfig checks the name `kubernetes` in the server's
  certificate, not the forward address. The run empties `access/` when
  the guests go down.

A host port reaches only what `vivarium.forward` names, and forwards are
fixed when a guest boots. A port map with no `host`, such as
`{ guest = 6443; }`, gets the guest's port on the host, or the first
free port above it; `access.json` says which. `ctl`, `switch` and
`reload` work on a ready environment as on a paused run. `continue` at
ready runs the phases a `reload` added, then holds again. Only `stop`
ends the environment.

Without the `vivarium` CLI, `nix run --file . checks.k8s-host.up -- --out
./k8s` runs the same environment in the foreground.

`down` stops it, waits for the run to exit and exits 0 if every phase
passed. `vivarium ctl --out ./k8s stop` does the same. A stop before
`ready` is an ordinary stop: exit 5. `ls` lists every run on this host
that has a control socket, with its state: `ready`, `paused` or
`running`. `vivarium monitor` exits 6 at `ready`. The MCP server's
`start` takes `hold=True`.

A sandboxed `nix build` of the same attribute never holds: it runs the
phases and gives the verdict, so the check proves the environment comes
up.

## Look at a guest's screen and use it

Give the guest a screen. It must be a QEMU guest:

```nix
nodes.desk = {
  vivarium.backend = "qemu";
  vivarium.display.enable = true;   # 1024x768; set width and height to change it
};
```

Then, in a phase or through `exec`:

```python
await desk.wait_for_text(r"login:")  # OCR; returns where the text is
await desk.send_chars("root\n")  # types on a US layout
await desk.send_key("ctrl-alt-f2")  # QEMU's sendkey names
await desk.click_text(r"^OK$")  # waits for it, clicks its middle
await desk.click(512, 384, button="right")  # pixels from the top left
await desk.drag((10, 10), (300, 200))
path = await desk.screenshot("after-login")  # a PNG in artifacts/desk/screenshots/
```

The MCP server's `screenshot(run, machine)` tool returns the image, so
an agent sees the screen and then acts on it through `exec`.

OCR misreads. Wait for lowercase words where you can: tesseract read
`VIVARIUM` in the console font as `UIUARIUM`. When a wait times out, the
error holds what OCR read and the path of a screenshot.

## Wait for a guest's state

Do not write a retry loop in a phase. Each `Machine` has these:

```python
await vm.wait_until_succeeds("ping -c 1 10.0.0.1", 90)  # until it exits 0
await vm.wait_until_fails("ping -c 1 10.0.0.1", 90)  # until it exits non-zero


async def up() -> dict[str, Any] | None:  # None: not yet
    peers = json.loads(await vm.succeed("vtysh -c 'show bgp summary json'"))
    return peers if all(p["state"] == "Established" for p in peers["peers"].values()) else None


peers = await vm.until("BGP up", up, 90)  # the first answer that is not None
await vm.never("a route back", route_back, 10)  # fails if it is not None within 10 s
await vms.until("BGP up", lambda vm: up_on(vm), 90)  # every guest at once
```

- `until` returns the first answer that is not `None`. `False` and `{}`
  are answers.
- At the timeout, `until` raises `MachineError`. Pass `evidence=` (an
  async function that returns text), and the error holds what it says.
- `never` always takes its whole timeout when it passes.
- `vms.until` takes a check that gets the guest. `machines=` limits it to
  some guests. Its error names each guest that did not come good.
- Each wait is one step in `report.json`.

## Find out what went wrong

```console
$ jq -r 'select(.kind=="phase_finished") | "\(.phase)\t\(.data.state)"' out/events.jsonl
$ jq -c 'select(.kind=="journal" and .data.priority <= 3)' out/events.jsonl   # every error, every guest
$ less out/console/one.log                                                     # one guest's console
```

[reference.md](reference.md#what-a-run-leaves-behind) lists every file a
run writes.

## Run a test from an agent

With MCP, `vivarium-mcp` starts runs and reaches into them. nixidae's CLAUDE.md
lists its tools. `start` returns two monitor commands:

- `monitor`: pauses, failures and the verdict; exits 4 at a pause, 5 for a stopped run.
  Run it bare: a pipe or a `; echo` after it hides that exit status, and a
  pause then reads as success.
- `monitor_all`: every event, through pauses to the verdict.

An agent that only runs shell commands does not need MCP. With the
`vivarium` CLI, one command starts the run and waits:

```console
$ vivarium run checks.breakpoint --out ./out --break-on-failure --until-pause
$ vivarium ctl --out ./out continue --wait
```

`--until-pause` starts the run in its own session, with its output in
`<out>/run.log`, and exits at the first pause or at the verdict: 4 at a
pause, then 0, 1, 2 or 5 as the monitor exits. The run and its guests
stay up after a pause. `continue --wait` does the same from a pause.

With a `nix run` driver instead:

1. Start the run in the background, with its output in a file.
2. Start `vivarium monitor <out> --quiet` in the background at the same
   time. It waits for the run, and exits at a pause or at the verdict.
3. Read `<out>/run.json` for the commands: the run writes it before the
   first guest boots. Do not scrape the output file for them.
4. At a pause, copy the `exec` line from the file, change the Python,
   and run it.
5. Run the `continue` line with `--wait`. It exits at the next pause or
   at the verdict, so there is no monitor to start again.

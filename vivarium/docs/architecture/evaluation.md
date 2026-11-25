# Evaluation: from `mkTest` to the spec

`mkTest` in `lib.nix` takes a module and returns a derivation with more
attributes beside it. Everything the run needs to know is decided here
and written into one JSON file, the spec. The runner reads only the spec.

```{graphviz} evaluation.dot
```

## The test module

`mkTest` calls `lib.evalModules` on three modules: `modules/run.nix`,
which declares the test's options, `modules/recipes`, the standard
recipes, and the caller's module. Every run imports the recipes. Only
`boot` is on by default.

The test module gives:

- `nodes.<name>` and `templates.<name>`: NixOS modules, one per guest
  and one per template.
- `phases.<name>`: a script or a pytest run, with `after`, `nodes`,
  `always` and `timeout`. `modules/run.nix` sorts them with
  `lib.toposort` into `ordered`. A cycle stops the evaluation, and so
  does an `after` that names no phase, a `nodes` that names no guest,
  or a phase with both a script and a pytest run, or neither.
- `knobs`: values that `modules/run.nix` reads from the environment with
  `builtins.getEnv` (`resolved`). So a knob changes the spec, and a
  sandboxed check reads the value the evaluation saw.
- `segments`, `router`, `settings`, `python`, `pythonPath`, `typeCheck`,
  `interactive` and `backend`.

## The router decision

`routerEnabled` decides whether the run has a router guest. When the
caller sets `router.enable`, that value decides, `false` included.
When it is null, the run has a router on macOS (except a UML router),
when a segment names a router network, or when a guest wants one.

To ask a guest whether it wants a router, `wantsRouter` evaluates that
guest a second time, with no router and with `_module.check = false`.
The guest's own evaluation cannot answer: there, `uplink.proxy` and
`uplink.segment` read the decision, and the answer would depend on
itself.

## Indexes

`indexOf` gives each guest its position in the sorted names of `nodes`,
from 0. The router takes 254, so a router added to a run moves no
guest's index. A template takes the next index after the nodes, in the
order of its name. The index decides:

- the MAC address of NIC `n`: `52:54:00:12:n:index`, both in hex
  (`MachineSpec.mac`);
- the ssh port `vivarium.sshPort`: 4325 + index;
- behind the router, the uplink address: `10.0.2.(15 + index)`.

An instance of a template gets its index from the runner when it is
launched (`free_index` in `vivarium_runner/instances.py`), not here.
It starts at the spec's `instanceBase`: the number of guests without
the router, plus the number of templates. The templates take the
indexes just below it. The router's index is 254, so it takes none of
them.

## Guests and templates

`evaluate` calls `mkNode` for each guest. `mkNode` is NixOS's
`eval-config.nix` with `modules/` imported. Each guest imports:

- `defaults`, the module every guest imports;
- its own module from `nodes.<name>`;
- `peersModule`, which writes each peer's address into `/etc/hosts`;
- `routerModule`: with a router, this puts the guest's uplink on the
  segment `uplink-<name>`, serves it from the router and adds the router
  to `vivarium.requires`. The guest with the router's name becomes the
  router.

Each guest receives every other guest's configuration as the module
argument `nodes`. The values are lazy, so a guest can read a peer's
address without a cycle. Templates get the same modules and `nodes`, and
`vivarium.template = true`.

## Checks

`checkedConfig` is the test module after these checks. A failed check
stops the evaluation with a message that names the guest or segment:

- every segment in `segments` has a member;
- a segment that names a router needs `router.enable`;
- no router network overlaps another or the uplink;
- the assertions of a guest that is not NixOS (a NixOS guest checks its
  own when its system is built).

`typeCheck` type checks every phase script and pytest directory with
zuban. The `.attempt` builder and each driver's environment
(`VIVARIUM_TYPECHECKED`) name its output, so Nix builds the check before
either one can run a script that fails it.

## The spec

`spec` writes `vivarium-<name>-spec.json`. It holds:

| Field | From |
| --- | --- |
| `name`, `settings`, `knobs` | the test module |
| `phases` | `ordered`: name, `after`, `always`, `nodes`, `timeout`, and a script path or a pytest directory |
| `machines`, `templates` | `machineSpec` for each guest and template, with `storePaths`, `lateClosure` and `requires` |
| `segments` | `hub`, `capture`, `instanceAddresses`. A captured segment and a template's segment are always hubs |
| `vivarium` | the Python environment whose `vivarium-runner` reads this spec |
| `unshare`, `pythonPath` | the `unshare` program, the phases' helper modules |

Three more checks run when `machines` is evaluated, so they stop the
evaluation of the spec and not of `checkedConfig`:

- no fixed host port is asked for twice (`inboundClashes`);
- no guest that only the router can serve is in a run with no router
  (`unrouted`);
- each name in `vivarium.requires` is a guest of the run.

`machineSpec` is the one place that turns a guest into JSON. The runner
reads the same fields with `MachineSpec.from_json`
(`vivarium_runner/machine_spec.py`), and the session reads the whole
file with `Spec.read` (`vivarium/spec.py`). No schema is shared between
the Nix writer and the Python readers (nixidae/nixidae#1444, A5).

## The outputs

The derivation `mkTest` returns is the sandboxed check. Its attributes:

| Attribute | What it is |
| --- | --- |
| `.attempt` | the run in the sandbox; never fails, writes `status` |
| `.driver`, `.driverDebug`, `.driverInteractive`, `.up` | the run by hand, through `wrap` |
| `.phases` | the program that lists the phases |
| `.spec`, `.nodes`, `.systems`, `.config` | the spec and the evaluated values |
| `.extend { modules; }` | `mkTest` again with more modules |
| `.uml`, `.qemu`, `.krun`, `.container` | `.extend` with every guest on that backend |

`.attempt` asks for the system features of every guest's backend
(`featuresFor`). A UML run asks for none, so it builds on any Linux
builder.

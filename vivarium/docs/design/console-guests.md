# Console guests: design

Carl and Claude write this together. Status: **proposal, not reviewed.**
nixidae/nixidae#945 (RouterOS) is the first user; #657 (other operating
systems) and #948 (containerlab's NOS recipes) are the wider frame.

## The problem

A vivarium guest today runs the vivarium agent. `Machine` reaches it over
a socketpair (UML `ssl0`, QEMU `virtconsole`), and every phase API
(`execute`, `unit_state`, `journal`, `add_closure`, ...) is an agent call.
The run itself also calls the agent on every guest, whatever the phases
ask: the injected `boot` phase (`systemctl is-system-running --wait`),
the settle marker (`systemd-cat`), and `systemctl poweroff` at the end.

A network operating system such as MikroTik RouterOS runs its own kernel
and userland. The agent cannot run there. The only channel the host has
at boot is the **serial console**: bytes in, bytes out, a login prompt
and a CLI. Every other channel (ssh, the REST API) exists only after
something has configured it through that console.

## Facts, measured

RouterOS CHR 7.24.5 (`chr-7.24.5.img`, raw, 128 MiB), QEMU with KVM,
256 MiB, serial on a unix socket, two virtio NICs, 2026-10-21. The raw
serial bytes are in `tests/fixtures/routeros/`, with the probe that
made them.

**Login**

- `CHR Login:` arrives 3.5 to 4.5 s after QEMU starts.
- Login options are suffixes on the user name. `admin+ct65535w9999h`:
  no colour (`c`), no terminal detection (`t`), 65535 columns, 9999
  rows. QEMU's serial line has no width; RouterOS's line editor wraps at
  the width the login gives it. At 80 (the default) and at 512 a
  3000-byte command wraps in the echo and in the output; at 65535
  neither wraps. With `9999h`, `print` does not page.
- Dumb mode (`e`) pads the prompt with spaces and loses the markers
  below. Not used.
- Without `t`, the login takes 25 s; with it, 15 s. The 15 s that remain
  fall between the password and the banner, on every boot. Cause
  unknown.
- Password empty. Then the licence question (`[Y/n]`), then a forced
  password change (ROS 7).

**Commands**

- The prompt is `\r\x1b[9999B[admin@<identity>] > `. It changes with
  the identity, so the parser cannot match a fixed prompt.
- The command echoes back, then output lines, each starting with `\r`.
- **No exit status.** An error is text, and a syntax error rejects the
  whole line: nothing on it runs.
- The line editor changes what it is given: a raw tab becomes two
  spaces (completion). `?` inside a quoted string is safe.
- `[[:parse "<s>"]]` runs a string as a script (`[:parse ...]` alone
  only builds it). `:onerror e in={...} do={...}` catches every error
  class measured, a syntax error inside the parsed string included, and
  gives its text in `$e`.
- `:execute script=<s> as-string` runs a script and returns its output
  as a string. It does **not** raise the script's errors to the caller:
  the error text comes back as output. (`as-string` must follow
  `script=`; the other order is a syntax error.)
- `:convert from=base64` and `to=base64` work on strings of at least
  179 KB.
- The line editor accepts a 24 KB line; a 76 KB line never returns.

**Hardware**

- NICs are named by PCI order: the first virtio-net is `ether1`, with
  the MAC QEMU gave it. `ether1` runs a DHCP client by default.
- An extra virtio disk holding a **bare ext4 filesystem** (no partition
  table) mounts at once as `pcie1`, read-write. Its files are under
  `pcie1/`. Attached read-only, the mount fails ("Permission denied").
- An ext4 filesystem inside an MBR partition table stops CHR from
  booting at all, even with `bootindex=0` on the CHR disk.
- `/import file-name=pcie1/cfg.rsc verbose=yes` applies the file and
  echoes each line as `#line N`.

## Proposal

Five layers. Each one is replaceable without the others.

### 1. Image (Nix)

`images.routeros-chr` fetches `chr-<v>.img.zip` from
download.mikrotik.com (fixed hash, pinned version: MikroTik keeps old
versions there) and unzips it. A local derivation (`preferLocalBuild`,
but see #947).

### 2. Guest module (Nix)

`vivarium.routeros = { image; config; }`, beside `vivarium.image` and
`vivarium.vyos`. QEMU backend only (assertion). The node is a NixOS
evaluation for the common options (memory, interfaces, forwards), and
its system is never built. It sets `system.build.vivariumImageGuest`
with `boot = "disk"` and `control = "routeros"`.

`config` is RouterOS script (`.rsc`) text. The module builds a **config
disk**: a bare ext4 image with `config.rsc` on it, made with
`mkfs.ext4 -d` as the UML root image is.

### 3. Backend: QEMU disk boot

A second branch in `Qemu._launch`, also #657's:

- The disk is the image, through a per-run qcow2 over it, with
  `bootindex=0`. The config disk follows, as a per-run copy, read-write
  (RouterOS refuses to mount it read-only).
- No `-kernel`, no virtiofs for the store or the artifacts, no balloon.
  `Machine.start` builds no store view (no `storePaths`).
- **The serial port is the control socket**: the guest end of the
  socketpair the agent uses today.
- The first NIC is the uplink (passt), so `ether1` gets the default DHCP
  client. Segment NICs follow in `interfaces` order, as `ether2..N`.

Console and control are one byte stream. The control layer writes what
it reads into the console log and `Machine`'s history. Nothing else
reads the serial port.

### 4. Control (Python, anyio)

`Machine` talks to a `Control`, not to the agent directly. The lifecycle
calls the run makes on every guest are part of it:

```python
class Control(Protocol):
    async def ready(self) -> None: ...          # the boot phase waits on this
    async def run(self, command: str, timeout: float) -> Result: ...
    async def mark(self, text: str) -> None: ...  # the settle marker
    async def shutdown(self) -> None: ...

@dataclass(frozen=True)
class Result:
    ok: bool
    output: str
```

- `AgentControl` wraps today's arpyc calls, unchanged behaviour. `ok` is
  `exit code == 0`.
- `ConsoleControl` drives a byte stream with a **dialect**. One command
  at a time (an `anyio.Lock`), deadlines with `anyio.fail_after`.

A dialect is pure functions over bytes, so it is tested against the
recorded transcripts with no VM:

```python
class Dialect(Protocol):
    login: Sequence[Step]                         # expect / send pairs
    def frame(self, command: str, nonce: str) -> bytes: ...
    def parse(self, buffer: bytes, nonce: str) -> Result | None: ...  # None: not complete
```

The RouterOS dialect:

- `login`: the steps above, user `admin+ct65535w9999h`, then a password
  set for the run.
- `frame`: **base64 in both directions.** The typed line is a fixed
  template; only the nonce and base64 vary, so no command can break its
  syntax, and the line editor sees nothing it rewrites:

  ```
  :put "<n>-B"; :put [:convert to=base64 [:execute script=":onerror e in={ [[:parse [:convert from=base64 \"<command as base64>\"]]] } do={ :put (\"<n>-E \" . \$e) }" as-string]]; :put "<n>-D"
  ```

  The executed script decodes the command, runs it, and on an error
  prints `<n>-E <message>` as its last line. Its whole output comes
  back base64-encoded, one line, so no `\r`, escape code or wrap can
  reach the data.
- `parse`: the base64 between the lines `<n>-B` and `<n>-D` (the echo
  holds the markers only inside quotes). Decoded, a last line starting
  `<n>-E ` means `ok = False` with that message; the lines before it
  are the output up to the error.
- A command whose base64 passes 16 KB goes in chunks: each chunk is
  appended to a `:global` with its own short line, and the frame decodes
  the global instead of a literal. The global is removed after.

Measured with this frame: interface output, a bad command, an invalid
value, an error after output in the middle of a script, a syntax error,
`:error`, a multi-line script, a bounded `/ping`, and 10000 lines of
output (179 KB, 2.9 s) all come back exact.
- `ready`: logged in, then `/import file-name=pcie1/config.rsc` through
  the frame. An import error fails the boot with RouterOS's message.
- `mark`: `:log info "<text>"`.
- `shutdown`: none. Nothing in the guest persists, so the backend kills
  QEMU. (`/system shutdown` asks `[y/N]`.)

### 5. Machine API

- `execute` / `succeed` / `fail` go to `Control.run`. On RouterOS the
  command is RouterOS CLI, not a shell.
- Agent-only calls (`unit_state`, `journal`, `add_closure`, `switch_to`,
  `listening`, `processes`, `meminfo`) raise `MachineError` naming the
  control kind, as `switch_to` does on a cloud image today.

## Rules for a phase on a console guest

- One command at a time per guest. A command that does not return
  (`/ping` without `count=`, `print follow`) holds the console until its
  deadline, then fails the phase. Give every such command a bound.
- Output is text for a human. Prefer `print terse`, `print as-value` or
  `:put [get ...]` to tables.

## Services later

Once the console works, `config` can enable the REST API (`/ip service
www-ssl`) on `ether1`, and a vivarium forward reaches it from the host.
A `RestControl` then gives phases JSON (`GET /rest/ip/address`) instead
of parsed text. The console stays the bootstrap and the fallback.

## Limits

- The free CHR licence caps each interface at 1 Mbit/s. Tests stay on
  the control plane (BGP, OSPF, routes, reachability). Throughput needs
  a trial licence and is out of scope.
- QEMU only, so a RouterOS test needs KVM, as every QEMU test does.
- About 20 s from start to a logged-in console, 15 s of it the login.

## Milestones

1. Image, module, config disk, QEMU disk boot, `Control` with
   `AgentControl` and `ConsoleControl`, the RouterOS dialect, and its
   unit tests on the transcripts. Test: CHR boots, imports a config,
   and `:put [/system identity get name]` answers.
2. Two NixOS guests route through the CHR.
3. BGP between the CHR and an FRR guest (#945's done line).
4. `RestControl`.

## Open, for Carl

- **`execute` or a new method.** `execute("/ip address print")` reuses
  every helper that takes `execute`, but means a different language per
  guest. A `cli()` method keeps `execute` a shell. Proposed: `execute`,
  because "the guest's own command language" is what a phase wants.
- **`Control` now, or a side class.** Proposed: `Control` now, with the
  agent path wrapped unchanged. It is the seam every later NOS uses, and
  the lifecycle calls need it anyway.
- **asyncio in `Machine`.** New code is anyio; `Machine` stays asyncio
  until its own rework (nixidae/nixidae#949), which this does not start.
- **The 15 s login.** Live with it for now; look again when a test boots
  many routers.

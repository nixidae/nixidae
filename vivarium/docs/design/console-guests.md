# Console guests: design

Carl and Claude write this together. Status: **milestone 1 built**
(2026-10-21): the `routeros` test boots CHR, applies its config and
answers through the console, sandboxed. nixidae/nixidae#945 (RouterOS)
is the first user; #657 (other operating systems), #948 (containerlab's
NOS recipes) and #950 (agents as computer users, replayed) are the wider
frame.

## The problem

A vivarium guest runs the vivarium agent. `Machine` reaches it over a
socketpair (UML `ssl0`, QEMU `virtconsole`), and every phase API
(`execute`, `unit_state`, `journal`, `add_closure`, ...) is an agent call.
The run itself also calls the agent on every guest, whatever the phases
ask: the `boot` recipe (`systemctl is-system-running --wait`), the settle
marker (`systemd-cat`), and `systemctl poweroff` at the end.

A network operating system such as MikroTik RouterOS runs its own kernel
and userland. The agent cannot run there. The only channel the host has
at boot is the **serial console**: bytes in, bytes out, a login prompt
and a CLI. Every other channel (ssh, the REST API) exists only after
something has configured it through that console.

## Facts, measured

RouterOS CHR 7.24.5 (`chr-7.24.5.img`, raw, 128 MiB), QEMU with KVM,
256 MiB, serial on a unix socket, two virtio NICs, 2026-10-21. The probe
and its raw serial bytes are in `tests/fixtures/routeros/`.

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

## Design

Five layers. Each one is replaceable without the others.

### 1. Image (Nix)

`images.routeros-chr` fetches `chr-<v>.img.zip` from download.mikrotik.com
(fixed hash, pinned version: MikroTik keeps old versions there).

### 2. Guest module (Nix): `modules/routeros-guest.nix`

`vivarium.routeros = { disk; config; }`, beside `vivarium.image` and
`vivarium.vyos`. QEMU backend only (assertion). The node is a NixOS
evaluation for the common options (memory, interfaces, forwards); its
system is never built. It sets `system.build.vivariumImageGuest` to
`{ control = "routeros"; configDisk; }`, and no store closure, so the
spec gives the guest no store view and no `qemuBoot`.

`config` is RouterOS script. The module writes it as
`vivarium-config.rsc` onto a bare ext4 image (`mkfs.ext4 -d`), after a
preamble that keeps vivarium's naming promise: each of
`vivarium.interfaces` is renamed, by the MAC vivarium gave it, from
CHR's `etherN` to its attribute name, and gets its `addresses`. The
uplink stays `ether1`.

### 3. Backend: QEMU disk boot (`Qemu._launch_disk`)

Taken when the spec's `boot` holds `image`. Also #657's.

- The disk is the image, through a per-run qcow2 over it, with
  `bootindex=0`. The config disk follows, as a per-run copy, read-write.
- No `-kernel`, no virtiofs, no balloon, no subordinate-id check.
- **The serial port is the control socket**: the end of the socketpair
  the agent would get.
- The first NIC is the uplink (passt), so `ether1` gets the default DHCP
  client. Segment NICs follow in `interfaces` order, as `ether2..N`.

### 4. Control (`vivarium_runner/control.py`, anyio)

`Machine.execute` runs every command through a `Control`:

```python
class Control(Protocol):
    async def execute(self, command: str, timeout: float,
                      env: Mapping[str, str] | None = None) -> tuple[int, str]: ...
    async def aclose(self) -> None: ...
```

- `AgentControl` (in `machine.py`) is the arpyc `run` call, unchanged:
  a shell, with its exit status.
- `ConsoleControl` drives a byte stream with a **dialect**: it answers
  the login screens, then sends frames and reads until the dialect parses
  a result. One command at a time (`anyio.Lock`), deadlines with
  `anyio.fail_after`. On a timeout it sends Ctrl-C and waits for the
  prompt; if none comes, every later command fails with that reason.
  Its exit status is 0, or 1 with the guest's message as the last line.
  Every byte read goes to `console.raw` among the guest's artifacts.

The agent-only calls (`unit_state`, `journal`, `listening`, ...) stay
agent RPC, outside `Control`. On a console guest they raise
`MachineError` naming the console; `add_closure` and `switch_to` refuse.

A dialect is data and pure functions, so it is tested with no VM:

```python
@dataclass(frozen=True)
class Dialect:
    name: str
    login: Sequence[Rule]          # a screen ending in `pattern` gets `reply`; None: done
    prompt: re.Pattern[bytes]
    frame: Callable[[str, str], list[bytes]]          # command, nonce -> lines
    parse: Callable[[bytes, str], Result | None]      # None: not complete
    configure: str | None = None   # applies the config disk after login
```

The RouterOS dialect (`vivarium_runner/routeros.py`):

- `login`: user `admin+ct65535w9999h`, empty password, no licence, then
  the password `vivarium`.
- `frame`: **base64 in both directions.** The typed line is a fixed
  template; only the nonce and base64 vary, so no command can break its
  syntax, and the line editor sees nothing it rewrites:

  ```
  :put "<n>-B"; :put [:convert to=base64 [:execute script=":onerror e in={ [[:parse [:convert from=base64 \"<command as base64>\"]]] } do={ :put (\"<n>-E \" . \$e) }" as-string]]; :put "<n>-D"
  ```

  The executed script decodes the command, runs it, and on an error
  prints `<n>-E <message>` as its last line. The whole output comes back
  as one base64 line, so no `\r`, escape code or wrap reaches the data.
  A command whose base64 passes 16 KB goes in chunks through a `:global`,
  which the frame line removes after.
- `parse`: the base64 between the lines `<n>-B` and `<n>-D` (the echo
  holds the markers only inside quotes), complete once the prompt is
  back. A last line `<n>-E <message>` means failure.
- `configure`: waits up to 30 s for `vivarium-config.rsc` on any disk,
  then `/import ... verbose=yes`. A failing line fails the boot.

Measured with this frame: interface output, a bad command, an invalid
value, an error after output in the middle of a script, a syntax error,
`:error`, a multi-line script, a bounded `/ping`, and 10000 lines of
output (179 KB, 2.9 s) all come back exact.

### 5. The run

- `Machine.start` on a console guest logs in, applies the config disk,
  and records the boot. The `boot` recipe asks systemd only on agent
  guests (`vm.control_kind`).
- The settle marker skips a guest whose journal is not streaming, which
  a console guest never does.
- Shutdown does not ask: nothing in the guest persists, so QEMU is
  stopped. (`/system shutdown` asks `[y/N]`.)

## Tests

- `pkgs/vivarium-runner/tests/test_routeros.py`: the frame, the parser,
  chunking, and `ConsoleControl` against a fake CHR (login, failure,
  chunked command, timeout recovery, a looping login); and every frame
  of a real run's `console.raw` (`tests/fixtures/`) parsed again.
- `routeros` (`default.nix`): CHR under QEMU, config disk, a command, a
  failing command's message, 3000 lines of output, a 36 KB chunked
  script, an agent call refused. 20 s, 19.4 of it the boot.
- `routeros-route`: two NixOS guests through the CHR.
- `routeros-bgp`: eBGP with FRR, and two LANs joined only by it.

## Rules for a phase on a console guest

- `execute` runs the guest's own command language, not a shell.
- One command at a time per guest. A command that does not return
  (`/ping` without `count=`, `print follow`) holds the console until its
  deadline, then fails the phase. Give every such command a bound.
- Output is text for a human. Prefer `print terse`, `print as-value` or
  `:put [get ...]` to tables.

## Later

- **Services.** `config` can enable the REST API (`/ip service www-ssl`)
  on `ether1`, and a vivarium forward reaches it from the host. A
  `RestControl` then gives phases JSON (`GET /rest/ip/address`). The
  console stays the bootstrap and the fallback.
- **A screen control** for #950: observe with a screenshot and OCR, act
  with keys and the pointer. The console transcript is the first
  recording format a replay reads.
- **`Machine` on anyio** (#949). `ConsoleControl` is anyio already.

## Limits

- The free CHR licence caps each interface at 1 Mbit/s. Tests stay on
  the control plane (BGP, OSPF, routes, reachability). Throughput needs
  a trial licence and is out of scope.
- QEMU only, so a RouterOS test needs KVM, as every QEMU test does.
- About 20 s from start to a logged-in console, 15 s of it the login.

## Milestones

1. **Done.** Image, module, config disk, QEMU disk boot, `Control`, the
   RouterOS dialect and its tests, the `routeros` test.
2. **Done.** Two NixOS guests route through the CHR (`routeros-route`).
   The config disk starts by renaming each NIC by its MAC to its
   vivarium name and adding its addresses.
3. **Done.** eBGP between the CHR and an FRR guest (`routeros-bgp`):
   each learns the other's LAN, and hosts on them meet at TTL 62. In
   7.24 the AS and router ID live on `/routing bgp instance`;
   `connection` takes `instance=`.
4. `RestControl`.

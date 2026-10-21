"""Probe RouterOS CHR over its serial console; keep every byte.

The measurements in docs/design/console-guests.md came from it. A
spike, not part of any build: run it in a shell with qemu_kvm and
python3. An environment variable picks the experiment: EDITOR_ONLY,
B64_ONLY, WRAP_ONLY, BIG_IN, or none for the login and disk probe.

usage: probe.py <workdir> <login-user> <chr.img> [extra disks...]
Writes <workdir>/transcript.raw (every byte read) and events.jsonl
(time, direction, text; large, since the serial port gives about a byte
per read).
"""
import json, os, re, socket, subprocess, sys, time

work, user, img, *extra = sys.argv[1:]
os.makedirs(work, exist_ok=True)
sock_path = os.path.join(work, "s")
argv = ["qemu-kvm", "-m", "256", "-nodefaults", "-display", "none",
        "-chardev", f"socket,id=s0,path={sock_path},server=on,wait=on", "-serial", "chardev:s0",
        "-drive", f"file={img},if=none,id=d0,format=raw",
        "-device", "virtio-blk-pci,drive=d0,bootindex=0",
        "-netdev", "user,id=n0", "-device", "virtio-net-pci,netdev=n0",
        "-netdev", "user,id=n1", "-device", "virtio-net-pci,netdev=n1"]
for d in extra:
    argv += ["-drive", f"file={d},if=virtio,format=raw"]
q = subprocess.Popen(argv, stderr=subprocess.DEVNULL)
for _ in range(50):
    if os.path.exists(sock_path):
        break
    time.sleep(0.1)
s = socket.socket(socket.AF_UNIX); s.connect(sock_path); s.settimeout(0.2)
raw = open(os.path.join(work, "transcript.raw"), "wb")
ev = open(os.path.join(work, "events.jsonl"), "w")
t0 = time.monotonic()
buf = b""

def note(direction: str, data: bytes) -> None:
    ev.write(json.dumps({"t": round(time.monotonic() - t0, 3), "dir": direction,
                         "text": data.decode("utf-8", "backslashreplace")}) + "\n")
    ev.flush()

def read_until(pat: bytes, timeout: float = 60) -> bytes | None:
    global buf
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        m = re.search(pat, buf)
        if m:
            out, buf = buf[:m.end()], buf[m.end():]
            return out
        try:
            d = s.recv(65536)
        except socket.timeout:
            continue
        if d:
            raw.write(d); raw.flush(); note("in", d); buf += d
    note("timeout", pat)
    print(f"TIMEOUT {pat!r} tail={buf[-300:]!r}")
    return None

def send(x: str) -> None:
    b = x.encode() + b"\r"
    note("out", b); s.sendall(b)

def step(label: str, cmd: str, pat: bytes = rb"\] > $", timeout: float = 30) -> None:
    t = time.monotonic()
    send(cmd)
    out = read_until(pat, timeout)
    print(f"=== {time.monotonic()-t0:6.1f}s (+{time.monotonic()-t:4.1f}) {label}\n{out!r}\n")

read_until(rb"Login: $", 90); print(f"login prompt at {time.monotonic()-t0:.1f}s")
t = time.monotonic(); send(user); read_until(rb"Password: $")
send(""); out = read_until(rb"\[Y/n\]: $|new password> $|\] > $", 60)
print(f"after password +{time.monotonic()-t:.1f}s")
if out and out.endswith(b"[Y/n]: "):
    send("n"); out = read_until(rb"new password> $|\] > $", 30)
if out and out.endswith(b"new password> "):
    send("vivarium"); read_until(rb"new password> $"); send("vivarium"); read_until(rb"\] > $")
print(f"logged in at {time.monotonic()-t0:.1f}s")

F = ':put "{n}-B"; :do {{ {call} }} on-error={{ :put "{n}-E" }}; :put "{n}-D"'
O = ':put "{n}-B"; :onerror e in={{ {call} }} do={{ :put "{n}-E $e" }}; :put "{n}-D"'
long = "x" * 3000
import base64
def b64(s: str) -> str:
    return base64.b64encode(s.encode()).decode()
B = (':put "{n}-B"; :onerror e in={{ :put [:convert to=base64 [:execute script=[:convert from=base64 "{p}"] as-string]] }}'
     ' do={{ :put "{n}-E $e" }}; :put "{n}-D"')
B2 = (':put "{n}-B"; :onerror e in={{ :put [:convert to=base64 [:execute script=[:convert from=base64 "{p}"] as-string]] }}'
      ' do={{ :put ("{n}-E " . [:convert to=base64 $e]) }}; :put "{n}-D"')
def W(n: str, script: str) -> str:
    # The executed script is fixed apart from base64 and the nonce, so
    # nothing the caller sends can break its syntax.
    inner = (f':onerror e in={{ [[:parse [:convert from=base64 \\"{b64(script)}\\"]]] }}'
             f' do={{ :put (\\"{n}-E \\" . \\$e) }}')
    return (f':put "{n}-B"; :put [:convert to=base64 [:execute script="{inner}" as-string]]; :put "{n}-D"')
if os.environ.get("BIG_IN"):
    for kb in (4, 16, 48, 128):
        body = ":local s 0\n" + "".join(f":set s ($s + {i})\n" for i in range(kb * 1024 // 16))
        body += ":put $s"
        print(f"payload {len(body)}B script, b64 {len(b64(body))}B")
        step(f"k {kb}KB", W("k", body), timeout=60)
    q.kill()
    raise SystemExit
if os.environ.get("WRAP_ONLY"):
    cmds = [
        ("a print", W("a", "/interface print terse")),
        ("b bad cmd", W("b", "/bogus")),
        ("c invalid value", W("c", "/ip address add address=bad interface=ether2")),
        ("d error mid-script", W("d", ':put "before"\n/bogus\n:put "after"')),
        ("e syntax error", W("e", ':put ("unclosed')),
        ("f :error", W("f", ':error "custom failure"')),
        ("g ok multi-line", W("g", ":local x 5\n:put ($x * 2)")),
        ("h 2000 lines", W("h", ':for i from=1 to=2000 do={ :put ("line number " . $i) }')),
        ("i 10000 lines", W("i", ':for i from=1 to=10000 do={ :put ("line number " . $i) }')),
        ("j ping bounded", W("j", "/ping 127.0.0.1 count=2")),
    ]
    for label, cmd in cmds:
        step(label, cmd, timeout=60)
    q.kill()
    raise SystemExit
if os.environ.get("B64_ONLY"):
    cmds = [
        ("a print", B.format(n="a", p=b64("/interface print terse"))),
        ("b bad cmd", B.format(n="b", p=b64("/bogus"))),
        ("c invalid value", B.format(n="c", p=b64("/ip address add address=bad interface=ether2"))),
        ("d tab and quote and dollar", B.format(n="d", p=b64(':put "a\tb \\"q\\" \\$x ?"'))),
        ("e multi-line script", B.format(n="e", p=b64(":local x 5\n:put ($x * 2)\n/system identity set name=b64\n:put [/system identity get name]"))),
        ("f :error", B2.format(n="f", p=b64(':error "custom failure"'))),
        ("g error mid-script", B2.format(n="g", p=b64(':put "before"\n/bogus\n:put "after"'))),
        ("h syntax error", B2.format(n="h", p=b64(':put ("unclosed'))),
        ("i big output", B.format(n="i", p=b64(':for i from=1 to=300 do={ :put ("line " . $i) }'))),
        ("j timing 40 adds", B.format(n="j", p=b64(':for i from=1 to=40 do={ /ip address add address=("10.60." . $i . ".1/24") interface=ether2 }; :put [:len [/ip address find]]'))),
    ]
    for label, cmd in cmds:
        step(label, cmd, timeout=30)
    q.kill()
    raise SystemExit
if os.environ.get("EDITOR_ONLY"):
    cmds = [
        ("Q long put (3000)", f':put "{long}"'),
        ("R long put via frame", O.format(n="R", call=f'[[:parse ":put \\"{long}\\""]]')),
        ("S question mark raw", ':put "a?b"'),
        ("T question mark escaped", ':put "a\\?b"'),
        ("U tab raw", ':put "a\tb"'),
        ("V tab escaped", ':put "a\\tb"'),
        ("W long add 40 addresses", ":for i from=1 to=40 do={ /ip address add address=(\"10.50.\" . $i . \".1/24\") interface=ether2 }"),
        ("X count", ":put [:len [/ip address find]]"),
    ]
    for label, cmd in cmds:
        step(label, cmd, timeout=20)
    q.kill()
    raise SystemExit
cmds = [
    ("version", ":put [/system resource get version]"),
    ("J onerror, bad cmd", O.format(n="J", call='[[:parse "/bogus"]]')),
    ("K onerror, invalid value", O.format(n="K", call='[[:parse "/ip address add address=bad interface=ether2"]]')),
    ("L onerror, no such item", O.format(n="L", call='[[:parse "/interface set nosuch mtu=1400"]]')),
    ("M onerror, ok print", O.format(n="M", call='[[:parse "/interface print terse"]]')),
    ("N onerror, :error", O.format(n="N", call=':error "custom failure"')),
    ("disks first", "/disk print terse"),
    ("files first", "/file print terse"),
    ("delay2", ":delay 3s"),
    ("files2", "/file print terse"),
    ("import", O.format(n="P", call='/import file-name=pcie1/cfg.rsc verbose=yes')),
    ("identity after import", "/system identity print"),
    ("addresses after import", "/ip address print terse"),
    ("A single-bracket parse, bad cmd", F.format(n="A", call='[:parse "/bogus"]')),
    ("B double-bracket parse, bad cmd", F.format(n="B", call='[[:parse "/bogus"]]')),
    ("C double-bracket parse, print", F.format(n="C", call='[[:parse "/ip address print"]]')),
    ("D local+call, print", F.format(n="D", call=':local c [:parse "/ip address print"]; $c')),
    ("E double-bracket, no such item", F.format(n="E", call='[[:parse "/interface set nosuch mtu=1400"]]')),
    ("F double-bracket, invalid value", F.format(n="F", call='[[:parse "/ip address add address=bad interface=ether2"]]')),
    ("G double-bracket, ok add", F.format(n="G", call='[[:parse "/ip address add address=10.9.0.1/24 interface=ether2"]]')),
    ("H double-bracket, quoted arg", F.format(n="H", call='[[:parse "/system identity set name=\\"r one\\""]]')),
    ("I print without terse (paging?)", "/interface print"),
    ("disks", "/disk print terse"),
    ("delay", ":delay 5s"),
    ("disks again", "/disk print terse"),
    ("files", "/file print terse"),
    ("identity", "/system identity print"),
]
for label, cmd in cmds:
    step(label, cmd)
q.kill()

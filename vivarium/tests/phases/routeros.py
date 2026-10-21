"""MikroTik RouterOS under QEMU, driven through its serial console.

The config disk applied at boot, a command's output, a failing command's
message, and an output too large for a terminal line.
"""

from vivarium_runner import MachineError, Machines


async def test(vms: Machines) -> None:
    r1 = vms.r1
    name = await r1.succeed(":put [/system identity get name]")
    if name.strip() != "r1":
        raise AssertionError(f"the identity is {name!r}; the config disk did not apply")
    addresses = await r1.succeed("/ip address print terse where interface=lo")
    if "10.255.0.1/32" not in addresses:
        raise AssertionError(f"lo has no 10.255.0.1/32:\n{addresses}")
    print("[test] the config disk applied: identity r1, 10.255.0.1/32 on lo")

    rc, out = await r1.execute("/ip address add address=bad interface=lo")
    if rc == 0 or "invalid value for argument address" not in out:
        raise AssertionError(f"a bad command returned {rc}: {out!r}")
    print(f"[test] a failing command fails, with RouterOS's message: {out.strip()}")

    lines = await r1.succeed(':for i from=1 to=3000 do={ :put ("line " . $i) }')
    if lines.splitlines() != [f"line {i}" for i in range(1, 3001)]:
        raise AssertionError("3000 lines of output did not come back whole")
    print("[test] 3000 lines of output came back whole")

    # Over 16 KB of base64, so the command goes in chunks through a global.
    script = "\n".join(f":set n ($n + {i})" for i in range(1, 2001))
    total = await r1.succeed(f":local n 0\n{script}\n:put $n")
    if total.strip() != str(sum(range(1, 2001))):
        raise AssertionError(f"a chunked script summed to {total!r}")
    left = await r1.succeed(":put [:len [/system script environment find where name~\"^viv\"]]")
    if left.strip() != "0":
        raise AssertionError(f"{left.strip()} chunk globals were left behind")
    print(f"[test] a {len(script) // 1024} KB script went in chunks and left no global")

    version = await r1.succeed(":put [/system resource get version]")
    print(f"[test] RouterOS {version.strip()}")

    try:
        await r1.unit_state("anything")
    except MachineError as error:
        if "console" not in str(error):
            raise
    else:
        raise AssertionError("an agent call on a console guest did not refuse")
    print("[test] an agent call refuses, naming the console")

"""SONiC VS under QEMU, driven through its serial console.

Milestone 1: the guest boots, the host logs in on the console, a shell
command's output comes back, and the SONiC CLI answers.
"""

from vivarium_runner import MachineError, Machines


async def test(vms: Machines) -> None:
    sw1 = vms.sw1

    who = await sw1.succeed("whoami")
    if who.strip() != "admin":
        raise AssertionError(f"whoami is {who!r}, not admin")
    print(f"[test] logged in as {who.strip()}")

    echoed = await sw1.succeed("echo vivarium-$((6 * 7))")
    if echoed.strip() != "vivarium-42":
        raise AssertionError(f"echo roundtrip returned {echoed!r}")
    print("[test] a command's stdout comes back whole")

    # A console guest has no exit status of its own: a failure is rc 1,
    # with the guest's message. The real status rides in that message.
    rc, out = await sw1.execute("exit 7")
    if rc != 1 or "exit status 7" not in out:
        raise AssertionError(f"`exit 7` reported ({rc}, {out!r})")
    print("[test] a failing command fails, with its status in the message")

    version = await sw1.succeed("show version", timeout=60)
    if "SONiC.202505" not in version:
        raise AssertionError(f"`show version` does not name the build:\n{version}")
    line = next(row for row in version.splitlines() if "SONiC Software Version" in row)
    print(f"[test] the SONiC CLI answers: {line.strip()}")

    try:
        await sw1.unit_state("anything")
    except MachineError as error:
        if "console" not in str(error):
            raise
    else:
        raise AssertionError("an agent call on a console guest did not refuse")
    print("[test] an agent call refuses, naming the console")

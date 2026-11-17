"""A dinix guest under krun: dinit is PID 1, the root is the disk, the store
is the host's overlay, and the agent answers for dinit."""

from vivarium_runner import Machines


async def test(vms: Machines) -> None:
    box = vms.box
    init = (await box.succeed("cat /proc/1/comm")).strip()
    if init != "dinit":
        raise AssertionError(f"PID 1 is {init!r}, not dinit")
    mounts = await box.succeed("cat /proc/mounts")
    # The kernel names a `root=` device /dev/root.
    for mount in ("/dev/root / ext4", "nix /host/nix virtiofs", "overlay /nix overlay"):
        if mount not in mounts:
            raise AssertionError(f"no {mount!r} in /proc/mounts:\n{mounts}")
    print("[test] dinit is PID 1 on the root disk, with the store overlay")

    # The boot phase returned, so `slow` had started: boot waits for it.
    slow = await box.succeed("dinitctl is-started slow")
    if slow.strip() != "STARTED":
        raise AssertionError(f"the boot phase ended with slow {slow.strip()}")
    print("[test] the boot phase waited for a service that takes a second")

    rc, out = await box.execute("dinitctl start broken")
    if rc == 0:
        raise AssertionError(f"broken started: {out}")
    report = await box.system_state(timeout=10)
    if report != {"state": "degraded", "failed": ("broken",)}:
        raise AssertionError(f"with broken failed, the guest reports {report}")
    print(f"[test] a failed service shows: {report}")

    # A daemon that exits 1 after it started: dinit calls it terminated.
    await box.succeed("dinitctl start dies")

    async def stopped() -> str | None:
        status = await box.succeed("dinitctl status dies")
        return status if "STOPPED" in status else None

    await box.until("dies to exit", stopped, timeout=10, poll=0.1)
    report = await box.system_state(timeout=10)
    if report != {"state": "degraded", "failed": ("broken", "dies")}:
        raise AssertionError(f"with dies exited 1, the guest reports {report}")
    print(f"[test] a daemon that exits 1 shows: {report}")

    try:
        await box.unit_state("vivarium-agent")
    except RuntimeError as error:
        if "asks systemd" not in str(error):
            raise
        print(f"[test] a systemd call refuses: {error}")
    else:
        raise AssertionError("unit_state answered on a dinit guest")

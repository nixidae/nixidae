"""A guest's screen, read, typed on and pointed at from the host.

`desk` shows a root shell on tty1. What is typed reaches the shell, and
what the shell prints comes back through OCR: the command says
`$((40+2))`, so a screen that reads 42 read the output, not the echo of
the keys. Lowercase words: tesseract reads the console font's capitals
badly (measured: VIVARIUM came back as UIUARIUM). The pointer is read
back from the tablet's evdev in the guest.
Each claim has its negative: the text is absent before it is typed, a
wait for text that never comes fails with what OCR read, and `blind`,
with no display, refuses.
"""

import re
import struct

import anyio
from vivarium_runner import Machine, MachineError, Machines

TOKEN = r"^answer 42$"
EV_KEY, EV_ABS, BTN_LEFT = 1, 3, 0x110


async def evdev(vm: Machine, device: str, count: int) -> list[tuple[int, int, int]]:
    """The next *count* events on *device*, as (type, code, value)."""
    out = await vm.succeed(
        f"timeout 10 dd if=/dev/input/{device} bs=24 count={count} status=none | od -An -tx1 -v | tr -d ' \\n'"
    )
    raw = bytes.fromhex(out.strip())
    return [struct.unpack("<qqHHi", raw[i : i + 24])[2:] for i in range(0, len(raw), 24)]


async def test(vms: Machines) -> None:
    desk, blind = vms.desk, vms.blind
    width, height = desk.screen_size

    await desk.wait_for_text(r"root@desk", timeout=120)
    shot = await desk.screenshot("prompt")
    data = shot.read_bytes()
    if data[:8] != b"\x89PNG\r\n\x1a\n" or struct.unpack(">II", data[16:24]) != (width, height):
        raise AssertionError(f"{shot} is not a {width}x{height} PNG")
    print(f"[test] {shot.name}: {width}x{height} PNG, the prompt on it read by OCR")

    if await desk.find_text(TOKEN):
        raise AssertionError(f"{TOKEN} is on the screen before anything was typed")
    await desk.send_chars("echo answer $((40+2)) | tee /root/typed\n")
    found = await desk.wait_for_text(TOKEN, timeout=60)
    typed = (await desk.succeed("cat /root/typed")).strip()
    if typed != "answer 42":
        raise AssertionError(f"the shell wrote {typed!r}")
    if not (0 <= found.left < width and 0 <= found.top < height):
        raise AssertionError(f"{found} is off the screen")
    print(f"[test] typed a command; the shell ran it and OCR read {found.text!r} at {found.center}")

    try:
        await desk.wait_for_text(r"NEVER-ON-SCREEN", timeout=2)
    except MachineError as error:
        if "OCR read" not in str(error):
            raise
    else:
        raise AssertionError("waiting for text that is not there passed")
    print("[test] a wait for text that never comes fails, with what OCR read")

    # The tablet's evdev node appears a moment after boot, so wait for its
    # own Handlers line to carry an event before reading, or the parse
    # below is a race. awk keeps to the tablet's block (reset at the blank
    # line), so it does not match a neighbour's event node.
    await desk.wait_until_succeeds(
        "awk '/Virtio Tablet/{f=1} /^$/{f=0} f&&/Handlers=.*event/' /proc/bus/input/devices | grep -q event"
    )
    devices = await desk.succeed("cat /proc/bus/input/devices")
    block = next(b for b in devices.split("\n\n") if "Virtio Tablet" in b)
    # `Handlers=event4` when the tablet has no mouse handler, so a word
    # split never sees one that starts with `event`.
    found = re.search(r"\bevent\d+", block)
    if found is None:
        raise AssertionError(f"no event node for the tablet in {block!r}")
    tablet = found[0]

    # Read the tablet's events while the click happens.
    captured: list[list[tuple[int, int, int]]] = []

    async def read() -> None:
        captured.append(await evdev(desk, tablet, 7))

    async with anyio.create_task_group() as group:
        group.start_soon(read)
        await anyio.sleep(1)
        await desk.click(width - 1, height - 1)
    events = captured[0]
    wanted = [(EV_ABS, 0, 0x7FFF), (EV_ABS, 1, 0x7FFF), (EV_KEY, BTN_LEFT, 1), (EV_KEY, BTN_LEFT, 0)]
    seen = [event for event in events if event[0] != 0]
    if seen != wanted:
        raise AssertionError(f"{tablet} saw {seen}, not {wanted}")
    print(f"[test] a click at the bottom right corner reached {tablet} as the axis maximum and BTN_LEFT")

    try:
        await blind.screenshot()
    except MachineError as error:
        if "vivarium.display.enable" not in str(error):
            raise
    else:
        raise AssertionError("a guest with no display took a screenshot")
    print(f"[test] {blind.name}, with no display, refuses a screenshot and names the option")

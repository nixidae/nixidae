#!/usr/bin/env agent-python
"""Set boot-args in a vmapple aux.img's NVRAM.

    nvram_bootargs.py AUX_IN AUX_OUT 'BOOT ARGS'

The NVRAM in aux.img is two banks at 0xA00000 and 0xA80000, 0x80000 bytes
each: an `nvram` header, an Adler-32 at +0x10 over +0x14 to the bank's end,
a generation at +0x14, then the `common` partition's NUL-separated
name=value strings. The bank with the higher generation is the live one.
The script sets the value in both banks and fixes each checksum. It refuses
when the new value does not fit in the zeros after `boot-args=`.
"""

from __future__ import annotations

import struct
import sys
import zlib
from pathlib import Path

BANKS = (0xA00000, 0xA80000)
BANK_SIZE = 0x80000
KEY = b"boot-args="


def main() -> None:
    src, dst, value = Path(sys.argv[1]), Path(sys.argv[2]), sys.argv[3].encode()
    data = bytearray(src.read_bytes())
    for bank in BANKS:
        if data[bank + 4 : bank + 10] != b"nvram\0":
            sys.exit(f"nvram_bootargs: no nvram header at {bank:#x}")
        end = bank + BANK_SIZE
        at = data.find(b"\0" + KEY, bank, end)
        if at < 0:
            sys.exit(f"nvram_bootargs: no boot-args in the bank at {bank:#x}")
        start = at + 1 + len(KEY)
        old_end = data.index(b"\0", start)
        # The new value, its NUL, and the NUL that ends the list.
        new_end = start + len(value) + 2
        if new_end > end or any(data[old_end:new_end]):
            sys.exit(f"nvram_bootargs: no room after boot-args at {bank:#x}")
        data[start:old_end] = bytes(old_end - start)
        data[start : start + len(value)] = value
        checksum = zlib.adler32(bytes(data[bank + 0x14 : end]))
        struct.pack_into("<I", data, bank + 0x10, checksum)
        print(f"bank {bank:#x}: boot-args={value.decode()!r}", file=sys.stderr)
    dst.write_bytes(data)


if __name__ == "__main__":
    main()

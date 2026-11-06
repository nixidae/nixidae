#!/bin/sh
# Spike: boot a tart macOS image on QEMU's vmapple machine under HVF.
# IMG: image directory (disk.raw, aux.img, config.json). ROM: AVPBooter.
# Extra arguments go to QEMU. The guest sees /dev/cu.vivarium-agent (host end
# $RUN/agent.sock) and vsock CID 3 (host end $RUN/vsock.sock, see
# hw/virtio/virtio-vsock-unix.c for the handshake).
set -eu
HERE=$(cd "$(dirname "$0")" && pwd)
IMG=${IMG:-$HERE/sonoma}
ROM=${ROM:-$HERE/AVPBooter.stock.bin}
QEMU=${QEMU:-$HOME/Dynamist/orchard/qemu/build-hvf/qemu-system-aarch64}
QEMU_IMG=${QEMU_IMG:-$HOME/Dynamist/orchard/qemu/build-hvf/qemu-img}
RUN=${RUN:-$HERE/run}
RAM=${RAM:-8G}
CPUS=${CPUS:-4}
mkdir -p "$RUN"

ECID=$(agent-python -I -c 'import base64,json,plistlib,sys
print(plistlib.loads(base64.b64decode(json.load(open(sys.argv[1]))["ecid"]))["ECID"])' "$IMG/config.json")

# The tart disk stays pristine: guest writes land in the overlay. The aux is a
# working copy, because iBoot writes NVRAM.
[ -e "$RUN/overlay.qcow2" ] || "$QEMU_IMG" create -q -f qcow2 -F raw -b "$IMG/disk.raw" "$RUN/overlay.qcow2"
[ -e "$RUN/aux.img" ] || cp "$IMG/aux.img" "$RUN/aux.img"

exec "$QEMU" \
  -M "${MACHINE:-vmapple}",uuid="$ECID",gfx=${GFX:-off} \
  -accel "${ACCEL:-hvf}" \
  ${CPU:+-cpu "$CPU"} \
  -m "$RAM" -smp "$CPUS" \
  -bios "$ROM" \
  -drive file="$RUN/aux.img",if=pflash,format=raw \
  -drive file="$IMG/disk.raw",if=pflash,format=raw,readonly=on \
  -drive file="$RUN/aux.img",if=none,id=aux,format=raw \
  -device vmapple-virtio-blk-pci,variant=aux,drive=aux,share-rw=on \
  -drive file="$RUN/overlay.qcow2",if=none,id=root,format=qcow2,cache=writeback,discard=unmap \
  -device vmapple-virtio-blk-pci,variant=root,drive=root \
  -netdev user,id=net0,hostfwd=tcp:127.0.0.1:2222-:22 \
  -device virtio-net-pci,netdev=net0 \
  -chardev file,id=ser0,path="$RUN/serial.txt" \
  -serial chardev:ser0 \
  -qmp unix:"$RUN/qmp.sock",server=on,wait=off \
  -device virtio-serial-pci,id=vser0 \
  -chardev socket,id=agent,path="$RUN/agent.sock",server=on,wait=off \
  -device virtserialport,bus=vser0.0,chardev=agent,name=vivarium-agent \
  -device virtio-vsock-unix-pci,guest-cid=3,path="$RUN/vsock.sock" \
  -display none \
  "$@"

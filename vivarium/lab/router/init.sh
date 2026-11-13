#!/bin/sh
# The router's /init. `lab=` on the kernel line picks the experiment.
mount -t proc proc /proc
mount -t sysfs sys /sys
mount -t devtmpfs dev /dev
arg() { sed -n "s/.*$1=\([a-z0-9-]*\).*/\1/p" /proc/cmdline; }
lab=$(arg lab)
block=$(arg block)
block=${block:-8192}
echo "router: up, lab=$lab block=$block"
case "$lab" in
  vsock-tx)
    # E1: stream zeros to the host's vsock port 5000.
    dd if=/dev/zero bs=1M count=4096 2>/dev/null | socat -b "$block" -u - VSOCK-CONNECT:2:5000
    echo "router: vsock-tx done"
    ;;
  local)
    # E1 baseline: the same pipe with no vsock.
    start=$(date +%s)
    dd if=/dev/zero bs=1M count=4096 2>/dev/null | socat -b "$block" -u - /dev/null
    echo "router: local done in $(($(date +%s) - start)) s"
    ;;
  *)
    echo "router: no lab"
    ;;
esac
poweroff -f

#!/bin/sh
# Boots a tart macOS image on vmapple under HVF and reports whether it works.
#
#   try_image.sh IMAGE_DIR RUN_DIR
#
# Fresh overlay in RUN_DIR. Disables tart's guest daemon at the first SSH
# login (it resets the guest without tart's host; see README), then counts
# guest resets for 3 minutes. Prints one line of results; evidence stays in
# RUN_DIR. Needs host/run-hvf.sh's QEMU, port 2222 free, and sshpass-free
# SSH through SSH_ASKPASS.
set -eu
HERE=$(cd "$(dirname "$0")" && pwd)
IMG=$(cd "$1" && pwd)
RUN=$2
rm -rf "$RUN"
mkdir -p "$RUN"
RUN=$(cd "$RUN" && pwd)
cp "$IMG/aux.img" "$RUN/aux.img"
printf '#!/bin/sh\necho admin\n' > "$RUN/askpass"
chmod +x "$RUN/askpass"

vm() {
    SSH_ASKPASS="$RUN/askpass" SSH_ASKPASS_REQUIRE=force DISPLAY=:0 \
        ssh -p 2222 -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null \
        -o LogLevel=ERROR -o ConnectTimeout=5 admin@127.0.0.1 "$@"
}

IMG=$IMG RUN=$RUN "$HERE/run-hvf.sh" > "$RUN/stdout.log" 2>&1 &
qemu=$!
start=$(date +%s)
first_ssh=
while [ $(( $(date +%s) - start )) -lt 600 ] && kill -0 "$qemu" 2>/dev/null; do
    if vm 'echo admin | sudo -S launchctl disable system/org.cirruslabs.tart-guest-daemon 2>/dev/null;
           echo admin | sudo -S launchctl bootout system/org.cirruslabs.tart-guest-daemon 2>/dev/null;
           sw_vers -productVersion' > "$RUN/first-ssh.txt" 2>&1; then
        first_ssh=$(( $(date +%s) - start ))
        break
    fi
    sleep 2
done
# grep -c prints 0 and exits 1 on no match; a missing file prints nothing.
boots() { n=$(grep -a -c "Start of iBootStage1" "$RUN/serial.txt" 2>/dev/null || true); echo "${n:-0}"; }
if [ -z "$first_ssh" ]; then
    agent-python -I "$HERE/qmp.py" "$RUN/qmp.sock" "info registers" > "$RUN/regs.txt" 2>&1 || true
    if kill "$qemu" 2>/dev/null; then
        why="no SSH in 600 s"
    else
        why="QEMU exited: $(grep -v stub "$RUN/stdout.log" | tail -1)"
    fi
    echo "$(basename "$IMG"): $why; boots=$(boots); pc=$(grep -o 'PC=[0-9a-f]*' "$RUN/regs.txt" | head -1); see $RUN"
    exit 1
fi
b0=$(boots)
resets=$(agent-python -I "$HERE/qmp_events.py" "$RUN/qmp.sock" 180 | grep -c RESET || true)
version=$(vm 'sw_vers -productVersion; sw_vers -buildVersion' 2>/dev/null | tr '\n' ' ')
up=$(vm uptime 2>/dev/null | sed 's/,.*//')
vm 'sync; sudo shutdown -h now' > /dev/null 2>&1 || true
wait "$qemu" 2>/dev/null || true
echo "$(basename "$IMG"): macOS $version; first SSH after ${first_ssh} s; boots before SSH $b0; resets in 180 s after: $resets; $up"

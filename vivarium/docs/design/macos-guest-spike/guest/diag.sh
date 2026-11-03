#!/bin/sh
# Runs at every boot: keeps the tail of this boot's log on disk.
d=/var/db/diag
mkdir -p "$d"
n=$(ls "$d" | grep -c '^boot-')
f="$d/boot-$n.log"
{
    date -u
    sysctl kern.bootreason kern.boottime 2>&1
    nvram -p 2>/dev/null | grep -a -i -E 'reason|panic|shutdown' | cut -c1-200
} > "$f"
log stream --style compact --level info \
    --predicate 'process == "kernel" OR process == "launchd" OR process == "powerd" OR eventMessage CONTAINS[c] "shutdown" OR eventMessage CONTAINS[c] "reboot" OR eventMessage CONTAINS[c] "restart"' \
    >> "$f" 2>&1 &
while :; do sync; sleep 1; done

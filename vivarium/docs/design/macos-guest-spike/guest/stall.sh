#!/bin/sh
# Finds the process whose thread is inside reboot() in the newest shutdown stall.
f=$(ls -t /Library/Logs/DiagnosticReports/shutdown_stall_* | head -1)
spindump -i "$f" -o /var/db/diag/stall.txt >/dev/null 2>&1
awk '/^Process:/ {proc=$0} /reboot|halt|PEHaltRestart|IOPMrootDomain|shutdown/ {print proc " :: " $0}' /var/db/diag/stall.txt \
    | sort | uniq -c | sort -rn | head -40 > /var/db/diag/stall-reboot.txt
sync

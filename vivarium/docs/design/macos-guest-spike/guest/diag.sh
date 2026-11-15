#!/bin/sh
# Runs as root at every boot (LaunchDaemon local.diag). Sends evidence to the
# host collector (host/collect.py) through slirp, so it outlives a reset:
#   9998: the newest shutdown-stall report, as spindump text
#   9999: the live unified log, through a pty so it is line-buffered
host=10.0.2.2
(
    f=$(ls -t /Library/Logs/DiagnosticReports/shutdown_stall_* 2>/dev/null | head -1)
    {
        echo "=== boot $(date -u) $(sysctl -n kern.boottime)"
        echo "=== stall $f"
    } > /tmp/stall.txt
    # spindump -i prints nothing until it is done; nc -w would time out.
    [ -n "$f" ] && spindump -i "$f" -o /tmp/stall-body.txt >/dev/null 2>&1
    cat /tmp/stall-body.txt >> /tmp/stall.txt 2>/dev/null
    nc -w 10 "$host" 9998 < /tmp/stall.txt
) &
# script copies its stdin into the pty: /dev/null would end the stream at once.
tail -f /dev/null | script -q /dev/null log stream --style compact --level debug | nc "$host" 9999

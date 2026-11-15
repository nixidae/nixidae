#!/bin/sh
# pynixd's darwin build VM: one build a connection, on vsock 3040, as root.
# Each line of the connection is one command; `run` is the last.
#
#   ping              answer "ok" (the host waits for this after a start)
#   mount TAG DIR     mount the virtiofs share TAG at DIR
#   union DIR         mount a tmpfs over DIR: a new file lands on the tmpfs,
#                     and the rest of DIR shows through
#   run ARG...        run ARG..., its output and errors on the connection
#
# A failed command answers "error: ..." and ends the connection. Words split on
# spaces: the host sends no argument with a space in it.
set -u
PATH=/usr/bin:/bin:/usr/sbin:/sbin

fail() {
    echo "error: $*"
    exit 1
}

while read -r command rest; do
    case "$command" in
    ping)
        echo ok
        exit 0
        ;;
    mount)
        set -- $rest
        [ $# -eq 2 ] || fail "mount takes TAG DIR: $rest"
        mkdir -p "$2" || fail "cannot make $2"
        out=$(mount_virtiofs "$1" "$2" 2>&1) || fail "mount_virtiofs $1 $2: $out"
        ;;
    union)
        out=$(mount_tmpfs -o union "$rest" 2>&1) || fail "mount_tmpfs $rest: $out"
        ;;
    run)
        set -- $rest
        exec "$@" 2>&1
        ;;
    *)
        fail "unknown command $command"
        ;;
    esac
done
fail "the connection ended before run"

#!/bin/sh
# The guest's store is the "Nix Upper" APFS volume, union-mounted on
# /nix/store: a name the volume lacks resolves in what it covers. That is the
# empty store directory of the "Nix Store" volume, or, after `host`, the
# host's store over virtiofs. macOS has no overlayfs; a store is flat and its
# paths never change, so one union level is enough.
#
#   builder-store.sh boot    mount the volume once /nix is up (LaunchDaemon)
#   builder-store.sh serve   one command a connection, on vsock 3032:
#     host        put the host store (virtiofs tag hoststore) under the volume
#     load-db     register what `nix-store --dump-db` wrote, from stdin
#     zerofree    zero the free pages, so a save is smaller
#     status      the mounts on /nix/store
#   Each answers "ok" or "error: ..." on its last line.
set -u
PATH=/usr/bin:/bin:/usr/sbin:/sbin
NIX=/nix/var/nix/profiles/default/bin

CACHE=/var/run/builder-store.dev

# diskutil takes 0.1 s a call, longer than the three mounts of `host`
# together, so the device node is looked up once and kept.
upper() {
    if [ -s "$CACHE" ]; then
        cat "$CACHE"
        return
    fi
    diskutil info "Nix Upper" 2>/dev/null | awk '/Device Node:/ {print $3}' | tee "$CACHE"
}

mount_upper() {
    dev=$(upper)
    [ -n "$dev" ] || { echo "error: no Nix Upper volume"; return 1; }
    # Mounted anywhere else (diskarbitration, /Volumes) it is busy for this mount.
    if mount | grep -q "^$dev on "; then
        umount "$dev" || diskutil unmount force "$dev" >/dev/null
    fi
    mount -t apfs -o union,nobrowse "$dev" /nix/store
}

upper_mounted() {
    mount | grep -q "^$(upper) on /nix/store "
}

case "${1:-}" in
boot)
    # A device node holds for one boot; a snapshot is of one boot.
    rm -f "$CACHE"
    # The Nix Store volume mounts up to 30 s after boot.
    for _ in $(seq 120); do [ -d /nix/store ] && break; sleep 0.5; done
    upper_mounted || mount_upper
    ;;
serve)
    read -r command
    case "$command" in
    host)
        if mount | grep -q '^virtio-fs on /nix/store '; then
            echo ok
            exit 0
        fi
        # Unmount the volume, put the share under, put the volume back.
        if upper_mounted && ! umount /nix/store; then
            echo "error: the store volume is busy"
            exit 1
        fi
        if ! out=$(mount_virtiofs hoststore /nix/store 2>&1); then
            mount_upper
            echo "error: mount_virtiofs: $out"
            exit 1
        fi
        mount_upper && echo ok
        ;;
    load-db)
        out=$("$NIX/nix-store" --load-db 2>&1) && echo ok || echo "error: $out"
        ;;
    zerofree)
        /usr/local/bin/zerofree && echo ok || echo "error: zerofree failed"
        ;;
    status)
        mount | grep ' on /nix/store '
        echo ok
        ;;
    *)
        echo "error: unknown command $command"
        ;;
    esac
    ;;
*)
    echo "usage: builder-store.sh boot | serve" >&2
    exit 2
    ;;
esac

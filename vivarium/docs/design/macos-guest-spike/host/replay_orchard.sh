#!/bin/sh
# Replays orchard's QEMU work as commits on the upstream QEMU commit it copied.
#
#   replay_orchard.sh QEMU_REPO ORCHARD_REPO
#
# orchard (github.com/yaelliethy/orchard) vendors QEMU as plain files. Its
# import matches upstream f8aef8a9 (2026-09-18) in every file except 19 it
# changed and 8 it added (host/find_qemu_base.py); 16 upstream files it left
# out (Rust .wrap files, tests/keys) stay as upstream has them. Each later
# orchard commit that touches qemu/ becomes one commit, with its author and
# date. The two vivarium commits come last, from orchard's local jj changes.
set -eu
Q=$1
O=$2
BASE=f8aef8a9aed7438083c400da10acabdec485dc9b
IMPORT=26710d37c7c024e769417a8e4f3161fa47a7ab8f
LATER="f86af39 ba2ac15 c73a2bd"
OURS="vnmstkqs mlumxrrw"   # jj change ids in orchard: vsock, then HVF

origin() {
    printf '\n\nImported from github.com/yaelliethy/orchard %s, qemu/ only.\n' "$1"
}

describe_as() {
    commit=$1
    jj -R "$Q" describe -m "$(git -C "$O" log -1 --format=%B "$commit")$(origin "$(git -C "$O" rev-parse "$commit")")"
    jj -R "$Q" metaedit \
        --author "$(git -C "$O" log -1 --format='%an <%ae>' "$commit")" \
        --author-timestamp "$(git -C "$O" log -1 --format=%aI "$commit")"
}

jj -R "$Q" new "$BASE"

# The import: every file whose content differs from the base, or is new.
git -C "$Q" ls-tree -r "$BASE" | awk -F'\t' '{split($1, m, " "); print m[3] " " $2}' | sort -k2 > /tmp/replay-base.$$
git -C "$O" ls-tree -r "$IMPORT:qemu" | awk -F'\t' '{split($1, m, " "); print m[3] " " $2}' | sort -k2 > /tmp/replay-import.$$
join -1 2 -2 2 -a 2 -e MISSING -o 0,1.1,2.1 /tmp/replay-base.$$ /tmp/replay-import.$$ |
    awk '$2 != $3 {print $1}' | while read -r path; do
        mkdir -p "$Q/$(dirname "$path")"
        git -C "$O" show "$IMPORT:qemu/$path" > "$Q/$path"
    done
rm -f /tmp/replay-base.$$ /tmp/replay-import.$$
describe_as "$IMPORT"

for commit in $LATER; do
    jj -R "$Q" new
    git -C "$O" diff --binary --relative=qemu "$commit^" "$commit" | git -C "$Q" apply
    describe_as "$commit"
done

for change in $OURS; do
    jj -R "$Q" new
    jj -R "$O" diff --git -r "$change" | git -C "$Q" apply -p2
    jj -R "$Q" describe -m "$(jj -R "$O" log --no-graph -r "$change" -T description)"
done
jj -R "$Q" new

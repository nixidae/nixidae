#!/usr/bin/env bash
#
# Write nix/sources.lock again, from nix/sources.nix and the recorded pointers.
#
# `umbrella status` says when the lock and a submodule pointer disagree. This
# is what answers it. It prints to standard output and writes nothing, so a
# run that goes wrong costs nothing:
#
#     nix/seed-lock.sh > nix/sources.lock.new && mv nix/sources.lock{.new,}
#
# Two arms, and they match the two arms of nix/resolve.nix.
#
# **A submodule takes the commit the umbrella records.** Not its working copy
# and not its branch head. The lock and the pointer are the pair that has to
# agree, and the pointer is the one a push already made public.
#
# **Everything else takes the head of the branch nix/sources.nix names.** So a
# run of this is an update for the third parties and a re-sync for our own.
# That includes nixpkgs, and moving nixpkgs rebuilds the world. Read the diff.
#
# This is a script and not `umbrella update` yet. The tool would have to
# evaluate Nix to read nix/sources.nix, and it depends on nothing but git and
# pygit2 today.
set -euo pipefail

root=$(git rev-parse --show-toplevel)
cd "$root"

# url and branch only. `path` is a Nix path, and asking for it here would
# copy every working copy into the store to answer.
spec=$(nix eval --json --file nix/sources.nix \
  --apply 'builtins.mapAttrs (_: v: { inherit (v) url branch; })')

# path -> the commit the umbrella records for it.
pointers=$(git ls-tree HEAD | awk '$2 == "commit" { print $3, $4 }')

out='{}'

for name in $(echo "$spec" | jq -r 'keys[]'); do
  url=$(echo "$spec" | jq -r --arg n "$name" '.[$n].url')
  branch=$(echo "$spec" | jq -r --arg n "$name" '.[$n].branch')

  # https://github.com/OWNER/REPO.git
  slug=${url#https://github.com/}
  slug=${slug%.git}
  owner=${slug%%/*}
  repo=${slug#*/}

  rev=$(echo "$pointers" | awk -v n="$name" '$2 == n { print $1 }')
  where="pointer"
  if [ -z "$rev" ]; then
    rev=$(git ls-remote "$url" "refs/heads/$branch" | cut -f1)
    where="$branch"
  fi
  if [ -z "$rev" ]; then
    echo "$name: no commit found on $branch at $url" >&2
    exit 1
  fi

  echo "  $name  $owner/$repo  $rev  ($where)" >&2
  locked=$(nix flake prefetch --json --refresh "github:$owner/$repo/$rev" |
    jq -c '(.locked | {type, owner, repo, rev, lastModified})
           + {narHash: (.locked.narHash // .hash)}')
  out=$(echo "$out" | jq --arg n "$name" --argjson v "$locked" '.[$n] = $v')
done

echo "$out" | jq -S '{version: 1, sources: .}'

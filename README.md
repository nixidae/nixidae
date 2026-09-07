# nixidae

One checkout that holds five repositories: four projects and the tool that
drives them.

| directory | what it is |
| --- | --- |
| `nanopynix` | Drive Nix from Python, batteries included |
| `pynixd` | The Nix daemon protocol in Python |
| `easykubenix` | Like kubenix, but easier |
| `nixkube` | A CSI driver for Nixxing Kubernetes |
| `umbrella` | The tool that drives this collection |

Each project keeps its own repository, its own history and its own remote.
The umbrella adds one thing: a known-good set of them, recorded as submodule
pointers. A change that crosses two projects can be made, built and tested in
one place, and the set that worked is written down.

## Start here

    git clone git@github.com:nixidae/nixidae.git
    cd nixidae
    nix run --file . umbrella -- initjj    # or initgit, for plain git

`--recurse-submodules` on the clone is optional. Either init checks out any
submodule that is missing, and neither touches one that is already there.

The choice of version control is per checkout, not per project. `initjj`
colocates each submodule as a jj repo and writes a marker inside `.git`, so
the choice is never committed and never shared. `initgit` leaves plain git.
`umbrella mode` shows or changes it.

The umbrella itself is always plain git. jj cannot record a submodule
pointer, so a jj umbrella could never do the one job an umbrella has.

## Every day

    umbrella status         # what is dirty, ahead, behind or unpushed
    umbrella status -f      # the same, after a fetch
    umbrella sync           # move the submodules onto the recorded pointers
    umbrella land -m "..."  # push the submodules, then record where they are

`land` is the one that publishes. It pushes each submodule that moved, then
stages the new pointer, and only then commits the umbrella. That order is
the guarantee: **the umbrella never records a commit that no remote has.**
The git hooks `initjj` installs enforce the same rule, so a commit or a push
made by hand cannot break it either.

## One more working copy

    umbrella wts add <name>     # the whole collection, again
    umbrella wts list
    umbrella wts rm <name>

A worktreespace is a git worktree of the umbrella plus one working copy of
each submodule, at the commit the umbrella records. A plain `git worktree
add` leaves every submodule empty, which gives a checkout nobody can build
in.

`.claude/settings.json` points Claude's worktree hooks at the same code, so
asking Claude for a worktree here gets the whole collection too.

A worktreespace is for throwaway work. It does not publish. Land from the
checkout it came from.

## Building

    nix build --file . umbrella
    nix-shell               # umbrella, jj and git

The four projects are not composed into one package set yet. Each carries
its own `default.nix`, its own flake inputs and its own dev shell, and the
signatures do not agree. Build each from its own directory, the way you did
before it moved in here. Every project has an `.envrc`, so direnv gets you
the right environment when you enter one.

Agreeing on that signature, and splitting the projects further, is the next
phase. Getting the collection into one checkout came first.

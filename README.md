# nixidae

One checkout that holds six repositories: four projects, the tool that
drives them, and the library they all evaluate through.

| directory | what it is |
| --- | --- |
| `nanopynix` | Drive Nix from Python, batteries included |
| `pynixd` | The Nix daemon protocol in Python |
| `easykubenix` | Like kubenix, but easier |
| `nixkube` | A CSI driver for Nixxing Kubernetes |
| `umbrella` | The tool that drives this collection |
| `flake-compatish` | Evaluate a flake without the flake evaluator |

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
    nix build --file . nanopynix.nanopynix
    nix build --file . easykubenix.manifestJSONFile
    nix build --file . pynixd.package
    nix build --file . nixkube.nixkube-docs
    nix-shell                       # umbrella, jj and git

Each project still has its own `default.nix`, its own `.envrc` and its own
dev shell. Entering a project directory gets that project's environment, and
a build started there gives the same answer as a build started here, because
it goes through the umbrella too. See below.

## The umbrella owns the inputs

Two files say where every source comes from, and each has one writer.

| file | holds | written by |
| --- | --- | --- |
| `nix/sources.nix` | the url, the branch, the optional working copy | a human |
| `nix/sources.lock` | the revision and the narHash | a tool |

`nix/resolve.nix` joins them and gives each name one value. `default.nix`
hands that set to each project as overrides, so a project's own `flake.nix`
still says what it needs and the umbrella says where it comes from.

The rule has two arms:

**A working copy wins.** When the spec names a directory and that directory
holds a `flake.nix`, the answer is the directory. Nix reads it where it lies,
nothing is copied to the store, and a change in one project is built by the
next with no commit, no push and no revision to bump. That is what the
collection is for.

**Otherwise the lock answers.** The revision becomes a pinned flake
reference. It is pure, so an evaluation that reaches no working copy needs no
`--impure` and no `NIX_PATH`.

The test is the `flake.nix` and not the directory. A clone made without
`--recurse-submodules` leaves every submodule directory present and empty,
and so does a tarball of this repository. A directory test would pick the
empty one.

**One nixpkgs.** Built alone, the four resolve four different ones from four
lock files. Here they share the revision in the lock, which is the revision
this machine's channel was on when the lock was written. Same content, same
derivations: `hello.drvPath` is `gx2drhxsvkh7xr490rg7dpqyn53iw3z0` either
way, so the pin costs no rebuild.

An override that does not apply is silent, so the wiring is checked. Every
name is judged, because a pinned reference lands on one store path and the
check can say which:

    nix build --file . checks.wired && cat result

### The three modes

| mode | what it is | how |
| --- | --- | --- |
| dev | local working copies, siblings from the umbrella | the default here |
| pinned-consistent | this repository at a revision, siblings from its lock | a checkout with no submodule contents |
| lock-faithful | every project reads its own `flake.lock` | `FLAKE_COMPATISH_DISABLE_OVERRIDES=1` |

The middle one is what a downstream consumer wants and it did not exist
before the lock. It does now: take this repository at a revision, do not
check the submodules out, and every sibling resolves to the revision that
revision recorded. Measured from an archive of a commit,
`nanopynix.nanopynix` is `kd9qc1536lii7l8lk724hd0d32l4aylx`, the same
derivation the working copies give.

The chain through easykubenix is not measured that way yet. It reads
nanopynix as a store path, and that path needs the fix in nanopynix that
this lock does not point at yet.

The third mode stays what it was. It reads each project's own lock, so
easykubenix builds a published nanopynix rather than the one next to it.
That is what a project's own CI sets, to make a `--file .` build agree with a
flake evaluation.

### The flat set beats the graph

The lock is a flat set of names and not a graph, and it does not need to be
one. Each of ours is evaluated as its own flake with the whole set behind
it, so every edge between them points at the copy here, at whatever depth.

That took a change to flake-compatish. An override replaces a node's source
and keeps the lock file's idea of that node's own inputs, and a lock file
can hold a second node for the same dependency: easykubenix's lock reaches
nanopynix, and that nanopynix took `flake-compatish_2`. An override is
matched by node name, so a name with a suffix was out of reach. Measured, it
was `/nix/store/91ak9bdm0a7b0kq6b40dhrvnmizgb8ky-source`.

`reroot` replaces the whole flake instead, keyed by the input name rather
than the node name, and the input has no suffix. `nix/wire.nix` builds that
set, and it refers to itself: nanopynix reads easykubenix from it and that
easykubenix reads nanopynix from it. Lazy, so the cycle costs nothing. Same
measurement now gives the working copy.

`nix/sources.nix` carries the `reroot` flag, so the set is the four projects
and nothing else. A third-party flake still reads its own lock below the
first level. Flag one to change that.

### What is still open

nixkube declares easykubenix `flake = false`, so it gets a directory rather
than a flake and calls it with no inputs. That copy asks for an umbrella of
its own. Inside this checkout it finds the right one; from a store path it
finds none and fetches the published one.

## The umbrella is the way in

A project does not wait to be called from here. Each `default.nix` asks the
umbrella for its inputs itself:

    nix build --file . nanopynix        # from here
    cd nanopynix && nix build --file .  # the same inputs

Inside this checkout a project finds `../nix/wire.nix` and uses it. Outside
it, nixidae is fetched with its submodules and the project puts its own
working copy in place of the submodule that came down. So a project cloned
on its own still builds through the umbrella, and still builds the source
the user is sitting in.

A project read as a store path is outside. `..` from a store path leaves the
store root and Nix refuses it, so the question is asked only of a working
copy.

`nix/wire.nix` is the one implementation, and both directions call it.

Two things follow.

**Nesting closes by itself.** easykubenix imports nanopynix, and that copy
asks the umbrella the same question, so it is the working copy in the next
directory. The nixkube to easykubenix to nanopynix chain is the same
derivation as `nanopynix.nanopynix` here. Both hops were published tarballs
before.

**A fetch from outside needs no key.** `.gitmodules` names the submodules by
`https://`, so a CI runner reaches them. This checkout still pushes over
SSH: `.gitmodules` says what a clone starts with, and `.git/config` holds
what this checkout uses.

`FLAKE_COMPATISH_DISABLE_OVERRIDES=1` turns all of this off and reads the
project's own lock, which is what its CI sets to make a `--file .` build
agree with a flake evaluation. A flake evaluation passes `inputs` itself and
never reaches the default at all.

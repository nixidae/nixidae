# nixidae

One checkout that holds six repositories: four projects, the tool that
drives them, and the library that reads other people's flakes.

| directory | what it is |
| --- | --- |
| `nanopynix` | Drive Nix from Python, batteries included |
| `pynixd` | The Nix daemon protocol in Python |
| `easykubenix` | Like kubenix, but easier |
| `nixkube` | A CSI driver for Nixxing Kubernetes |
| `umbrella` | The tool that drives this collection |
| `flake-compatish` | Read a flake we do not own |

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

## The umbrella owns the sources

**Nothing here builds through a flake.** Each `flake.nix` used to be 40 to
118 lines that declared its inputs and re-exposed what its `default.nix`
already returns, and the umbrella supplied those inputs anyway, so the
declaration decided nothing and the `flake.lock` beside it was a second pin
of sources this repository already pins.

Each project keeps a `flake.nix` even so, and it is a second door rather
than the way in. See below.

Two files say where every source comes from, and each has one writer.

| file | holds | written by |
| --- | --- | --- |
| `nix/sources.nix` | the url, the branch, the optional working copy | a human |
| `nix/sources.lock` | the revision and the narHash | a tool |

`nix/resolve.nix` joins them and `nix/wire.nix` turns the result into
directories, one per name. That set is what every project takes.

The rule has two arms:

**A working copy wins.** When the specification names a directory and that
directory has anything in it, the answer is the directory. Nix reads it
where it lies, nothing is copied to the store, and a change in one project
is built by the next with no commit, no push and no revision to bump. That
is what the collection is for.

**Otherwise the lock answers.** The revision becomes a pinned flake
reference. It is pure, so an evaluation that reaches no working copy needs
no `--impure` and no `NIX_PATH`.

The test is that the directory has something in it, not that it is there. A
clone made without `--recurse-submodules` leaves every submodule directory
present and empty, and so does a tarball of this repository.

**One nixpkgs.** Built alone, the four used to resolve four different ones
from four lock files. Here they share the revision in the lock, which is the
revision this machine's channel was on when the lock was written. Same
content, same derivations: `hello.drvPath` is
`gx2drhxsvkh7xr490rg7dpqyn53iw3z0` either way, so the pin costs no rebuild.

A source that comes from the wrong place is quiet, so it is checked:

    nix build --file . checks.wired && cat result

### Calling a flake, without being one

A repository we do not own is often a flake, and sometimes its outputs are
the only way in. `nix/call-flake.nix` reads one, with these sources in place
of whatever its own lock names. It is the only place a flake is read.

Nothing uses it today. Every third party here has a plain entry point, and
the last one to need a flake was treefmt-nix: `inputs.treefmt-nix.lib.mkWrapper`
became `(import sources.treefmt-nix).mkWrapper`, which takes the same two
arguments.

### A door for a flake consumer

Each project keeps a `flake.nix`, and it is not how the project builds. It
names a curated set of outputs and calls `default.nix`. Flakes have the
market share, so a consumer who writes `inputs.nanopynix.url` should get
something rather than nothing.

Two inputs, and neither duplicates a pin the umbrella keeps.

| input | what it decides |
| --- | --- |
| `nixpkgs` | the consumer's, handed to the umbrella in place of the lock's |
| `nixidae` | which umbrella, and nothing else. `flake = false` |

So the nixpkgs a consumer follows reaches every source, and every other
source comes from the umbrella revision that lock names. Two nodes in the
lock file.

Measured on pynixd. With its own nixpkgs the flake gives
`v55z1vv1dwv86naakwvhb9gr33svyzrp`; with the revision `nix/sources.lock`
names it gives `d3w8gnxlihcrdvz56fwqlwm4k4r3p6ba`, which is what `--file .`
gives. So the override reaches through, and the two doors agree when they
are given the same nixpkgs.

`nix/sources.nix` cannot be used from a flake: it finds the umbrella by an
impure fetch, and a pure evaluation refuses one. That is why `nixidae` is an
input rather than a lookup.

The lock files are not committed yet. Each has to name a pushed nixidae.

### The two modes

| mode | what it is | how |
| --- | --- | --- |
| dev | local working copies, the rest from the lock | the default here |
| pinned-consistent | this repository at a revision, everything from its lock | a checkout with no submodule contents |

There used to be a third, `FLAKE_COMPATISH_DISABLE_OVERRIDES=1`, which made
each project read its own `flake.lock` for everything. No lock file decides
anything now: a project's own one names an umbrella and a nixpkgs, and that
umbrella decides the rest.

The second one is what a downstream consumer wants: take this repository at
a revision, do not check the submodules out, and every name resolves to the
revision that revision recorded. Measured from an archive of an earlier
commit, `nanopynix.nanopynix` is `kd9qc1536lii7l8lk724hd0d32l4aylx`, the
same derivation the working copies give.

**It does not work at this commit.** `nix/sources.lock` still names the
revisions the siblings were on before they stopped being flakes, and those
copies call `nix/wire.nix` with arguments it no longer takes. The lock has
to be written again from the pushed revisions, and only a push can do that:
the submodules are ahead of both the pointer and the lock. `umbrella status`
shows the first half of that and not yet the second.

## The umbrella is the way in

A project does not wait to be called from here. Each one asks the umbrella
for the sources itself, in its own `nix/sources.nix`:

    nix build --file . nanopynix        # from here
    cd nanopynix && nix build --file .  # the same sources

Inside this checkout a project finds `../../nix/wire.nix` and uses it.
Outside it, nixidae is fetched and the project puts its own working copy in
place of the copy that came down. So a project cloned on its own still
builds through the umbrella, and still builds the source the user is sitting
in.

A plain tarball is enough for that fetch. `nix/sources.lock` is a file in
this repository's own tree, so a fetch that leaves the submodule directories
empty still resolves all of them.

A project read as a store path is outside. `..` from a store path leaves the
store root and Nix refuses it, so the question is asked only of a working
copy.

**Nesting closes by itself.** easykubenix hands nanopynix the same set
rather than letting it ask again, and nixkube does the same for easykubenix.
The chain is one nanopynix, and it is
`kd9qc1536lii7l8lk724hd0d32l4aylx` whichever end you start from.

**A fetch from outside needs no key.** `.gitmodules` names the submodules by
`https://`, so a CI runner reaches them. This checkout still pushes over
SSH: `.gitmodules` says what a clone starts with, and `.git/config` holds
what this checkout uses.

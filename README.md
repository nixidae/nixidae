# nixidae

One checkout that holds seven repositories: four projects, the tool that
drives them, the library that reads other people's flakes, and the one that
boots a NixOS guest without KVM so a test can be a derivation.

| directory | what it is |
| --- | --- |
| `nanopynix` | Drive Nix from Python, batteries included |
| `pynixd` | The Nix daemon protocol in Python |
| `easykubenix` | Like kubenix, but easier |
| `nixkube` | A CSI driver for Nixxing Kubernetes |
| `umbrella` | The tool that drives this collection |
| `flake-compatish` | Read a flake we do not own |
| `user-mode-nixos` | NixOS integration tests on User-Mode Linux |

Each project keeps its own repository, its own history and its own remote.
The umbrella adds one thing: a known-good set of them, written down in
`nix/sources.lock`. A change that crosses two projects can be made, built and
tested in one place, and the set that worked is recorded.

There are no submodules. A working copy here is an ordinary clone in an
ignored directory, and nothing about it is committed. That is what lets the
umbrella itself be a jj repo, and it means the lock is the only record there
is -- there is no second one to drift from.

## Start here

    git clone git@github.com:nixidae/nixidae.git
    cd nixidae
    nix run --file . umbrella -- init --jj   # or init, for plain git
    nix run --file . umbrella -- fetch --all # or name the ones you want

A fresh clone has no working copies, and that is the normal shape: every
source resolves from the lock, which costs a store path and no checkout.
`fetch` is how you start working on one.

The choice of version control is per checkout, not per project. `init --jj`
colocates each working copy as a jj repo and writes a marker inside `.git`, so
the choice is never committed and never shared. `init` alone leaves plain git.
`umbrella mode` shows or changes it.

If you already have a checkout from before the submodules went away, run
`umbrella init` once. It moves each repository out of `.git/modules` and into
its own working copy. Until then their whole history sits inside the
umbrella's `.git`, where one `rm -rf` takes every one of them.

## Every day

    umbrella status         # what is dirty, ahead, behind or unpushed
    umbrella status -f      # the same, after a fetch
    umbrella fetch <name>   # clone one source, at the locked revision
    umbrella sync           # move the working copies onto the locked revisions
    umbrella land           # push the working copies, then lock what was pushed
    umbrella update         # follow the branch each source declares

`land` is the one that publishes. It pushes each working copy that moved and
only then writes the revision into the lock. That order is the guarantee:
**the lock never names a commit that no remote has.** The git hooks `init`
installs enforce the same rule for a commit or a push made by hand.

`land` writes the lock and stops. Committing the umbrella is yours to do,
because `git commit` and `jj commit` are different commands and picking one
here would be wrong in the other.

jj runs neither hook. Under a jj umbrella they are feedback for whoever is on
plain git, and `umbrella status` reports an unpushed revision either way.

## One more working copy

    umbrella wts add <name>     # the whole collection, again
    umbrella wts list
    umbrella wts rm <name>

A worktreespace is a git worktree of the umbrella plus one working copy of
each source, at the revision that commit's lock names. A plain `git worktree
add` gives an umbrella with no working copies at all.

It needs the umbrella to be plain git. A jj workspace has no `.git` of its
own, and that is where the markers live, so `wts add` is refused under a jj
umbrella. Use `jj workspace add`; the sources beside it are ordinary clones
and need nothing.

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
    nix build --file . user-mode-nixos.lan
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

`user-mode-nixos` is the exception, and it is here as a checkout rather than
as a wired-in dependency. It needs nixpkgs and nothing else in this
collection, so its `default.nix` takes a package set and its `flake.nix` has
one input. The umbrella hands it the same nixpkgs the rest get, but it never
asks for one.

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
tarball of this repository has none of them, and neither does a fresh clone.

**A working copy is read at the revision the lock names, not as a directory.**
`nix/resolve.nix` builds a `git+file://` reference to the checkout. Nothing
reaches the network, and the store path is the one CI built: measured, a
`git+file://` fetch of a revision and a `github:` fetch of the same revision
give the identical store path. Reading the directory does not, so every
derivation below it differed and CI's cache held nothing a local render asked
for.

**`UMBRELLA_DEV` is the arm that diverges,** and it is opt in because it has
to be:

    UMBRELLA_DEV=1 nix build --file . nixkube.nixkube        # every source
    UMBRELLA_DEV=nixkube,pynixd nix build --file . ...       # these two

Those read the directories, so an uncommitted edit in one project reaches the
next build of another with no commit and no push. `builtins.getEnv` answers
`""` in a pure evaluation, so the reproducible arm is what an evaluation gets
unless somebody asked for the other one.

**One source at a time.** The arms are per name, not per checkout, so a source
with no working copy comes from the store while its neighbours come from disk.
Nobody works on all seven at once, so fetch the ones you want and leave the
rest:

    umbrella fetch nixkube    # clone it, at the locked revision
    umbrella status           # the ones with no working copy are listed apart

Removing a working copy is `rm -rf`, because nothing records it. Check
`umbrella status` first: a commit on no remote branch goes with it.

**One nixpkgs.** Built alone, the four used to resolve four different ones
from four lock files. Here they share the revision in the lock, which is the
revision this machine's channel was on when the lock was written. Same
content, same derivations: `hello.drvPath` is
`gx2drhxsvkh7xr490rg7dpqyn53iw3z0` either way, so the pin costs no rebuild.

A source that comes from the wrong place is quiet, so it is checked:

    nix build --file . checks.wired && cat result

There used to be a second record to disagree with -- a submodule pointer in
the umbrella's tree -- and a row of `umbrella status` that reported the drift.
There is one record now, so there is nothing to compare.

Two commands write it, and they are two because they answer different
questions:

    umbrella update             # every source follows its declared branch
    umbrella update nixpkgs     # one, and nothing else is touched
    umbrella update -n          # say what would move, write nothing
    umbrella land               # push what is on this disk, then lock that

`update` follows the forge. That includes nixpkgs, and moving nixpkgs rebuilds
the world, so read the diff. `land` publishes what is here. Use `land` for a
project you are working on: `update` would take the branch head and step over
the commit you have not pushed.

### Calling a flake, without being one

A repository we do not own is often a flake, and sometimes its outputs are
the only way in. `nix/call-flake.nix` reads one by name, with these sources
in place of whatever its own lock names. It is the only place a flake is
read.

That is a real thing to want in an impure evaluation. `nix flake` would make
the caller write the override as a `follows` in a lock file of their own;
this takes an attribute set, so the caller decides at the call site and
needs no lock.

    nix build --file . checks.callFlake && cat result

`nix/examples/call-flake.nix` is the worked example and the gate over it.
Three flakes, and each shows something the next does not:

| flake | what it shows |
| --- | --- |
| `treefmt-nix` | the override applies, and `lib.mkWrapper` is built and run |
| `pyproject-nix` | the same on a second flake — nothing is specific to the first |
| `tree-sitter-nix` | `nixpkgs` from here, `flake-utils` from its own lock |

The third one is the rule worth stating: **an override set does not have to
be complete.** A name the umbrella carries is replaced; a name it does not
is not an error and is not left unresolved, because the flake's own lock
answers for it.

The gate goes red. `FLAKE_COMPATISH_DISABLE_OVERRIDES=1` turns the overrides
off, all three fall back to their own locks, and the failure names which.

Nothing else uses it. Every third party here has a plain entry point, and
the last one to need a flake was treefmt-nix itself:
`inputs.treefmt-nix.lib.mkWrapper` became
`(import sources.treefmt-nix).mkWrapper`, which takes the same two
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
| pinned-consistent | this repository at a revision, everything from its lock | a checkout with no working copies |

There used to be a third, `FLAKE_COMPATISH_DISABLE_OVERRIDES=1`, which made
each project read its own `flake.lock` for everything. No lock file decides
anything now: a project's own one names an umbrella and a nixpkgs, and that
umbrella decides the rest.

The second one is what a downstream consumer wants: take this repository at a
revision, fetch nothing, and every name resolves to the revision that revision
locked. Measured from an archive of an earlier commit,
`nanopynix.nanopynix` is `kd9qc1536lii7l8lk724hd0d32l4aylx`, the same
derivation the working copies give.

That is now the default shape of a clone rather than something to arrange.

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

A plain tarball is enough for that fetch. `nix/sources.lock` is a file in this
repository's own tree, and the working copies are ignored, so a tarball with
none of them still resolves all of them.

A project read as a store path is outside. `..` from a store path leaves the
store root and Nix refuses it, so the question is asked only of a working
copy.

**Nesting closes by itself.** easykubenix hands nanopynix the same set
rather than letting it ask again, and nixkube does the same for easykubenix.
The chain is one nanopynix, and it is
`kd9qc1536lii7l8lk724hd0d32l4aylx` whichever end you start from.

**A fetch from outside needs no key.** `nix/sources.nix` names every source by
`https://`, so a CI runner reaches them. This checkout still pushes over SSH:
`url.insteadOf` in the user's own gitconfig rewrites what `umbrella fetch`
clones, and each working copy keeps whatever remote it was cloned with.

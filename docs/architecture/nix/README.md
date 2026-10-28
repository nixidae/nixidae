# How the Nix code of nixidae comes together

nixidae is one repository with eleven projects in it, called sources. josh
exports each source to a repository of its own. So each source builds in two
places: here, beside its siblings, and alone, from its exported repository.
The Nix code below makes both builds give the same derivations.

`sources.dot` draws the arrows this document explains. To render it:

```sh
nix shell --file '<nixpkgs>' graphviz --command dot -Tsvg docs/architecture/nix/sources.dot -o sources.svg
```

## The files

| File | What it does |
| --- | --- |
| `flake.nix` | Declares every pin. Every input is `flake = false`. |
| `flake.lock` | Holds the revision of each pin. |
| `nix/compat.nix` | Reads `flake.lock` without flakes: one `fetchTree` per node. |
| `nix/members.nix` | Names the sources, and the repository each one exports to. |
| `nix/wire.nix` | Gives `sources`: every source and every pin, as a directory. |
| `nix/call-flake.nix` | Reads a flake we do not own, with our pins in place of its lock. |
| `default.nix` | The root. Imports each source, and holds the root checks. |
| `<source>/default.nix` | The source's own build. Takes `sources`. |
| `<source>/nix/sources.nix` | Finds `sources` for a source that runs alone. |
| `<source>/flake.nix` | The public surface for flake users. Holds no logic. |
| `<source>/flake.lock` | Names the nixidae commit the exported source builds against. |

## Pins

`flake.nix` declares a pin, and `nix flake update <name>` moves it. Every
input is `flake = false`, so no pin brings a lock of its own, and
`flake.lock` stays flat. `nix/compat.nix` reads that lock for `default.nix`.
The flake and a build from the file therefore read one lock.

kr8s, containerd and flake-compatish are pins, not sources. An ignored
checkout at `./kr8s`, `./containerd` or `./flake-compatish` replaces its pin.
`nix/wire.nix` reads what git tracks in that checkout.

## `sources`: one directory per source and per pin

`nix/wire.nix` returns an attribute set. Each source of `nix/members.nix` is
the store path of what git tracks in its directory, named `source`. Each pin
is its fetched tree. A fetch of the source's exported repository gives the
same store path, so the two builds agree. A change in one source does not
change the store path of another.

In a checkout, `wire.nix` reads the tree with `fetchGit`. An uncommitted edit
counts. A new file counts after a jj command snapshots it, because jj marks
it intent-to-add in git's index. A jj workspace without `.git` cannot be
read: `jj workspace add --colocate` makes one that can.

## The root and the sources

The root `default.nix` imports each source and passes `sources`:

```nix
easykubenix = import sources.easykubenix { inherit pkgs sources system; };
```

**Always pass `sources`.** Without it, a source reads its own
`nix/sources.nix`. From a store path, that file cannot find the checkout, so
it fetches the nixidae commit its lock names: a second, older set of sources.

A source called alone takes `sources ? import ./nix/sources.nix`. That file
checks where it is:

- **In the nixidae tree** (`../../nix/wire.nix` exists): it calls the root's
  `wire.nix`.
- **Exported:** it reads its own `flake.lock`, fetches the nixidae commit it
  names, and calls that commit's `wire.nix`. It passes
  `overrides.<name> = <its own tree>`, so the source builds from its own
  files against its siblings at that commit.

Every source's `nix/sources.nix` comes from one template,
`nix/shared/sources.nix.in`. `shared-config-update` writes it (see below).

## Exported repositories

A push to `main` runs the `josh export` workflow:

1. `lock-subtrees --check` fails unless every source's `flake.lock` names a
   commit that only lock files have changed since. `lock-subtrees` writes
   those locks; the landing steps in `CLAUDE.md` run it.
2. `josh-export --verify` evaluates each exported tree as a flake, against
   the nixidae commit its lock names. The package `nix/members.nix` names must
   be the same derivation as the root attribute it names.
3. `josh-export --push` pushes each source's history, with its own commit
   hashes, to its repository.

josh runs in CI only. Its libgit2 refuses the `relativeworktrees` extension
of a colocated jj workspace.

## Files that Nix writes, and checks

Some committed files come from Nix. Each has a command that writes it and a
root check that fails when the committed file differs:

| Written | From | Command | Check |
| --- | --- | --- | --- |
| `.github/workflows/<source>-*.yml` | each source's `ciTestWorkflows`, through `nix/ci.nix` and ghanix | `nix run --file . ci-workflow-update` | `checks.ciWorkflows` |
| `<source>/nix/shared/ruff*.toml` | `nix/shared/`, through `nix/shared-config.nix` | `nix run --file . shared-config-update` | `checks.sharedConfig` |
| `<source>/nix/sources.nix` | `nix/shared/sources.nix.in`, through `nix/shared-config.nix` | `nix run --file . shared-config-update` | `checks.sharedConfig` |

Edit the source, run the command, and commit both. Never edit the written
file by hand.

## Lanes: one build per Nix

huggorm binds Nix, so everything that links huggorm comes once for each Nix
it supports. huggorm calls these lanes: `nix_2_34`, `nix_2_35`, `git`,
`determinate`, and a sanitizer variant of each. nanopynix adds two names,
`stable` and `latest`.

A source whose outputs link huggorm exposes `lanes.<lane>`, and the root
collects them:

```nix
(import nixidae { }).lanes.determinate.ekn   # ekn on Determinate Nix
```

`lanes.stable` is the same set of derivations as the top-level attributes.
nixidae/nixidae#1108 lists which sources take part, and why the others do
not.

## Calling a flake we do not own

`nix/call-flake.nix` takes a pin's name and returns that flake's outputs. It
replaces the flake's own lock with our pins, through flake-compatish, so the
flake builds against what this repository tested. `checks.callFlake` is both
the gate and the example.

## Known gaps

- The sources take different arguments and return differently shaped
  attribute sets. nixidae/nixidae#1103 proposes one schema.
- Each source has its own documentation build. nixidae/nixidae#1105 proposes
  one toolchain and a nixidae site.

# A third ecosystem: npm

The npm lane plans a project from its `package-lock.json`. Like the Rust
lane, it uses only `pydd.core` and the root wrapper `lib/core/root.nix`.

    root = pydd.node.npm { project = ./.; };

The result is an ordinary derivation. It holds the project in
`lib/node_modules/<name>` and its programs in `bin`. It takes `override`
over the arguments, `overrideAttrs` and the install hooks.

## The lock is the plan

A Python lock says nothing of how to build a package, and a Cargo lock
says nothing of units. An npm lock of version 2 or 3 says almost
everything. Its `packages` map holds one entry for each path that npm
installs, with the URL, the hash, the platform (`os`, `cpu`, `libc`),
`dev`, `optional`, `bin` and `hasInstallScript`.

So the planner runs no npm. It reads the lock in the build, and decides
three things for the platform of the build:

- **Which packages install.** An optional package whose `os`, `cpu` or
  `libc` does not fit is left out, as npm leaves it out. esbuild has one
  such package for each platform.
- **What each package can `require`.** Node looks for
  `node_modules/<name>` in the directory of the package, then in each
  parent directory. The planner walks the lock's paths the same way, and
  a workspace link resolves to its member.
- **What the result leaves out.** `omit` is npm's `--omit`, `[ "dev" ]`
  by default.

The host evaluation never reads the lock. Its cost does not grow with the
number of packages.

## One node per package

| entry | nodes | output |
| --- | --- | --- |
| a package | a fixed-output fetch, `ms-2.1.3.tgz` | the tarball |
|  | a node, `ms-2.1.3` | the unpacked package |
| a package with an install script | the same two | the package after `preinstall`, `install` and `postinstall` |
| the project | `<name>` | the project, its tree and its programs |

**The planner writes the fetches.** The Python and Cargo lanes fetch on
the host. A fetch here is a `Fixed` node from the planner, with the hash
of the lock. Its path is known before it builds (`fixed_output_path` in
`pydd.core`), so the next node names the path itself, not a placeholder.

**A node holds no dependency.** Node resolves a module from its real
path. A package linked from the store looks for its dependencies beside
the store path, and finds none. So a node holds only its own package,
and the root copies each node to the path that the lock gives it. Node
then resolves every `require` as it would after `npm ci`, with nested
versions too. The cost is disk space at build time; the store's
deduplication shares the files.

The other layout gives each node links to its dependencies, as pnpm
does. npm's trees have cycles, and a derivation cannot depend on itself,
so that layout needs one node for each cycle. This layout needs none.

## Install scripts

A package with `hasInstallScript` runs `preinstall`, `install` and
`postinstall` in its own node. A package with `binding.gyp` and no such
script runs `node-gyp rebuild`, as npm does. The node first copies the
part of the tree that the script can `require`: every package that the
lock resolves from the package, transitively, with each program in
`node_modules/.bin`.

The script gets what npm's run-script gives it, without a network:

| variable | value |
| --- | --- |
| `PATH` | each `node_modules/.bin` above the package, `node-gyp`, Node, stdenv's tools, a C compiler, Python |
| `npm_config_nodedir` | Node of the menu, so node-gyp downloads no headers |
| `npm_config_build_from_source` | `true`, so prebuild-install and node-pre-gyp download no binary |
| `npm_package_name`, `npm_package_version`, `npm_lifecycle_event` | as npm sets them |

`tests/node/native-npm` builds better-sqlite3. Its script tries
`prebuild-install`, which refuses, and then node-gyp compiles the SQLite
that the package bundles.

Install scripts that depend on each other in a cycle are refused, with
the cycle in the message. npm runs them in an order of its own choice.

## Native inputs

nixpkgs has no table of npm packages and their inputs, as
`defaultCrateOverrides` is for crates. So the lane has no hints. Two
sources give a package its tools, and the later one wins:

| source | example |
| --- | --- |
| the project | `"pydd": { "buildTools": { "canvas": ["pkg-config", "cairo.dev", "cairo"] } }` in `package.json` |
| the call | `buildTools.canvas = [ pkgs.pkg-config pkgs.cairo.dev pkgs.cairo ];`, `overrides.<package>.env` |

A tool reaches only the install script of its package, the same way as in
the Rust lane (`tool_env` of `pydd.core`).

## The project

The root copies the project, places every node, and links each workspace
member where the lock names it. It runs each script of `scripts`
(`[ "build" ]`) with `npm run --if-present`, in the project and in each
workspace member. It then removes what `omit` leaves out, and links each
program of the project and of its workspace members into `bin`.

A program's `#!/usr/bin/env node` becomes the path of Node, as stdenv's
`patchShebangs` does. The packages' programs get the same.

## The modes

Each project of `tests/node` gives one result in four modes:
`nix run --file . test-node`.

| mode | Nix | writes through |
| --- | --- | --- |
| `cli` | master, `builder-rpc-v0` | `nix derivation add` |
| `huggorm` | master, `builder-rpc-v0` | huggorm |
| `drv` | master, `builder-rpc-v0` | `pydd-drv` |
| `recursive` | 2.35, recursive-nix | `nix derivation add`, root to `$out` |

**`out` is possible for npm, and not written.** In `out`, the host makes
every node. Only cargo, in a build, knows Rust's units, so the Rust lane
cannot have `out`. The npm lock names every node, so the host could make
them all, as the Python lane does with `graph.nix`. The graph is then
static, as nixpkgs' `importNpmLock` already gives. The difference
between the ecosystems is whether the lock is the whole plan.

## What is missing

- Git dependencies and remote tarballs without a hash. The planner refuses
  them by name.
- `npm test` as `root.tests`.
- bun and pnpm locks, which would read into the same graph.

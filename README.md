# nixidae

One repository that holds nine projects, and the organisation's `.github`
repository. A change that crosses two of them
lands in one commit, and one lock pins everything they use from outside.

| directory | what it is |
| --- | --- |
| `nanopynix` | Drive Nix from Python, batteries included |
| `pynixd` | The Nix daemon protocol in Python |
| `easykubenix` | Like kubenix, but easier |
| `nixkube` | A CSI driver for Nixxing Kubernetes |
| `vivarium` | NixOS integration tests without KVM: UML, QEMU and container guests |
| `huggorm` | Nix C++ bindings generated from a Python DSL |
| `havsorm` | Salt states written in Nix, evaluated by huggorm |
| `pydd` | Dynamic derivations planned in Python |
| `ghanix` | GitHub Actions workflows as NixOS modules |
| `dotgithub` | `nixidae/.github`: the organisation profile and community files |

## Each project still has its own repository

josh exports each directory to its own repository on every push to `main`,
with the same commit hashes that repository always had. Work happens here:
a commit pushed straight to an exported repository does not come back, and
the next export refuses to overwrite it.

Each one goes to `github.com/nixidae/<directory>`, and `dotgithub` goes to
`github.com/nixidae/.github`.

`nix/members.nix` is the list. `.github/workflows/josh-export.yml` runs the
export, through `pkgs/josh-export`. Tests run only here. An exported
repository runs one workflow, the one that deploys its docs site.

An exported project builds on its own. Its `nix/sources.nix` reads the
nixidae commit from its `flake.lock`, fetches it, and puts its own tree in
the place of its copy there. `pkgs/lock-subtrees` writes those locks: each
one names the commit under it, because a commit cannot name itself. So the
locks land as a commit of their own.

## Landing

    jj commit -m ...
    nix run --file . lock-subtrees      # commits the locks, if a source moved
    jj bookmark set main -r @-
    jj git push --bookmark main

Then read the export run. `lock-subtrees --check` fails the export when a
lock names a commit with other changes after it.

## Building

    nix build --file . nanopynix.nanopynix
    nix build --file . pynixd.package
    nix build --file . nixkube.nixkube-docs
    nix build --file . vivarium.lan
    nix build --file . pydd.pydd
    nix-shell                       # jj and git

Each project keeps its own `default.nix`, `.envrc` and dev shell. A build
started in a project's directory gives the same derivations as one started
here: inside this tree, the project's `nix/sources.nix` calls the root
`nix/wire.nix`.

## The sources

`nix/wire.nix` gives every project one set of directories, one per name.

**A project here is what git tracks of its directory,** copied to a store
path named `source`. `fetchGit` of the working copy decides what that is, so
an uncommitted edit counts, and so does a new file, because jj marks it
intent-to-add in git's index. A `result` link or a scratch file does not. A
fetch of the project's own repository gives the same tree under the same
name, so a build here, in CI and in an exported repository read one input,
and an edit in one project does not change another's source.

**Everything else comes from `flake.lock`.** Every input is `flake = false`,
so the lock is flat, and `nix/compat.nix` reads it with one `fetchTree` per
node. The flake and a build from the file read one lock. Move a pin with:

    nix flake update nixpkgs

**A fork for upstream is a checkout, not a project.** kr8s and containerd
carry patches that must each stand as a pull request upstream, so they stay
in their own repositories. A checkout of one at `./kr8s` or `./containerd`,
which git ignores, takes the place of its pin. So does one at
`./flake-compatish`.

## Calling a flake, without being one

A repository we do not own is often a flake, and sometimes its outputs are
the only way in. `nix/call-flake.nix` reads one by name, with these sources
in place of whatever its own lock names. It is the only place a flake is
read.

    nix build --file . checks.callFlake && cat result

`nix/examples/call-flake.nix` is the worked example and the gate over it.

| flake | what it shows |
| --- | --- |
| `treefmt-nix` | the override applies, and `lib.mkWrapper` is built and run |
| `pyproject-nix` | the same on a second flake |
| `tree-sitter-nix` | `nixpkgs` from here, `flake-utils` from its own lock |

**An override set does not have to be complete.** A name we carry is
replaced. A name we do not carry comes from the flake's own lock.

`FLAKE_COMPATISH_DISABLE_OVERRIDES=1` turns the overrides off, and the gate
goes red and names which flake fell back.

## CI

Each project writes its test workflows as Nix values, `ciTestWorkflows`.
`nix/ci.nix` renders each one to `.github/workflows/<project>-<name>.yml`,
with the project's directory as the working directory of every step.

    nix run --file . ci-workflow-update
    nix build --file . checks.ciWorkflows   # fails on drift

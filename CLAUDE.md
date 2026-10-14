# nixidae

One repository, nine sources (`nix/members.nix`). josh exports each source to its own repository on every push to `main`, with the source's original commit hashes. nixidae/nixidae#4.

## Rules

- Commit here only. A push straight to an exported repository is not brought back, and the next export refuses to overwrite it.
- One tracker: nixidae/nixidae. Exported repositories keep their old issues; new ones go here.
- Tests run here only. An exported repository runs its Pages workflow and nothing else.
- kr8s, containerd and flake-compatish are not sources. Each is a pin in `flake.lock`; an ignored checkout at `./<name>` replaces the pin. kr8s and containerd patches must stand as upstream pull requests.
- A source's test workflows are `ciTestWorkflows` in its `default.nix`. Edit those, then `nix run --file . ci-workflow-update`. Never edit `.github/workflows/<source>-*.yml` by hand.
- A sibling import passes `sources`: `import sources.<name> { inherit pkgs sources; }`. Without it the sibling reads its own `nix/sources.nix` from its store copy and fetches the nixidae commit its lock names: a second, older set of sources.
- jj: a new file reaches a build only after a jj command has snapshotted it (it is then intent-to-add in git's index, which `fetchGit` reads).

## Landing

```sh
jj commit -m ...                    # the work, in as many commits as it needs
nix run --file . lock-subtrees      # commits the locks if a source moved
jj bookmark set main -r @-
jj git push --bookmark main
```

Then read the `josh export` run. It runs `lock-subtrees --check` and `josh-export --verify --push`.

## Gates

```sh
nix build --file . checks.ciWorkflows checks.callFlake checks.licenses
nix run --file . lock-subtrees -- --check
nix run --file . josh-export -- HEAD --verify     # no push without --push
```

Each source keeps its own gates; its own `CLAUDE.md` or `AGENTS.md` names them.

# nixidae

One repository, twelve sources (`nix/members.nix`): eleven projects and `dotgithub`, the organisation's `.github` repository. josh exports each source to its own repository on every push to `main`, with the source's original commit hashes. nixidae/nixidae#4.

## Rules

- Commit here only. A push straight to an exported repository is not brought back, and the next export refuses to overwrite it.
- One tracker: nixidae/nixidae. Every issue of the exported repositories moved here on 2026-10-19. Every exported repository has issues and pull requests turned off; a new source's repository is created that way (`gh api -X PATCH repos/nixidae/<source> -F has_issues=false -F has_pull_requests=false`). An old `nixidae/<source>#N` URL redirects, but `gh issue view` on it fails: use `gh api repos/nixidae/<source>/issues/N --jq .html_url`, or `nix/issue-migration/map.jsonl`.
- Labels: one `source: <name>` per issue (`source: monorepo` for josh, lock-subtrees, shared CI and pins), plus `upstream` or `needs decision` when they apply. The kind is the issue type (Bug, Feature, Task), not a label.
- `user-initiated`: Agent never starts it; User does. Sub-items the issue marks for agents are open (#866: nanopynix to huggorm).
- `off-main`: work on its own bookmark in its own workspace; nothing lands on `main` (#868: fix).
- Tests run here only. An exported repository runs its Pages workflow and nothing else.
- kr8s, containerd and flake-compatish are not sources. Each is a pin in `flake.lock`; an ignored checkout at `./<name>` replaces the pin. kr8s and containerd patches must stand as upstream pull requests.
- Shared tool configuration lives in `nix/shared/`. Edit it there, then `nix run --file . shared-config-update`; never edit `<source>/nix/shared/` or `<source>/nix/sources.nix` by hand (the template is `nix/shared/sources.nix.in`).
- Nix is formatted with nixfmt: `nix run --file . nixfmt-update`. `checks.nixfmt` fails on an unformatted tracked `.nix` file.
- Nix is linted with statix (`nix/statix.toml`): `checks.statix`. `nix run --file . statix-fix` applies its fixes, and drops comments inside parentheses it removes: diff the comments after it.
- A source's test workflows are `ciTestWorkflows` in its `default.nix`. Edit those, then `nix run --file . ci-workflow-update`. Never edit `.github/workflows/<source>-*.yml` by hand.
- A sibling import passes `sources`: `import sources.<name> { inherit pkgs sources; }`. Without it the sibling reads its own `nix/sources.nix` from its store copy and fetches the nixidae commit its lock names: a second, older set of sources.
- jj: a new file reaches a build only after a jj command has snapshotted it (it is then intent-to-add in git's index, which `fetchGit` reads).
- jj workspaces: add one with `jj workspace add --colocate` (jj >= 0.46), so it gets a git worktree that `fetchGit` reads. A workspace without `.git` refuses to evaluate; `jj git colocation enable` converts it.

## Landing

```sh
jj commit -m ...                    # the work, in as many commits as it needs
nix run --file . lock-subtrees      # commits the locks if a source moved
jj bookmark set main -r @-
jj git push --bookmark main
```

The push starts the `josh export` run: `lock-subtrees --check` and `josh-export --verify --push`. Landing ends at the push; do not wait for that run. josh runs in CI only: its vendored libgit2 refuses the `relativeworktrees` extension that a colocated jj workspace sets.

Landing from a secondary workspace: then advance the default workspace, `~/Code/nixidae`, onto the new `main` (jj skill, `references/workspaces.md`, "Landing a workspace"). croshome builds nixidae from that checkout, not from its lock, so a stale checkout builds stale packages with no error.

## Gates

```sh
nix build --file . checks.ciWorkflows checks.callFlake checks.licenses checks.pythonpath checks.mypyIni checks.sharedConfig checks.nixfmt checks.statix
nix run --file . lock-subtrees -- --check
```

Each source keeps its own gates; its own `CLAUDE.md` or `AGENTS.md` names them.

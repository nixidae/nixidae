# lillecarl

## Version control

Use jj, never git, for every version control operation, in every source.

- Finish a task with `jj commit -m "..."`. A fixup for the last commit: `jj squash --use-destination-message`, or `jj squash -m "..."` for a new message.
- Every `jj` command takes `--no-pager`.
- Divide a mixed working copy with `jj split <paths> -m "..."`.
- Finish each task on an empty commit: `jj new` after its last commit, so the next task does not land inside it.
- Git only when User asks for it, or a tool needs git plumbing.
- Subagents never use VCS tools: no `jj`, no `git`. They read and write files and run the gates, and report a failure to the primary agent.

## Landing

Landing from a secondary workspace: then advance the default workspace, `~/Code/nixidae`, onto the new `main` (jj skill, `references/workspaces.md`, "Landing a workspace"). croshome builds nixidae from that checkout, not from its lock, so a stale checkout builds stale packages with no error.

## Dev shells

Enter a source's dev shell through direnv: `direnv exec . <command>` from the source's directory (`direnv exec . pytest`, `direnv exec . ruff check --fix`).

- nanopynix: `direnv exec .` serves a cached environment, and huggorm's checkout is not a file direnv watches. After an engine change (yours, or one from an update), run `direnv reload` (a minute or two, works non-interactively). Measured: a stale shell held 83 settings and no `pure-eval`, and five tests failed that CI passed; after the reload, 113 and all pass.
- nixkube: `direnv allow && direnv reload`, or a new terminal.

## Output

- Never pipe away the output of a `checks.*` build or of `pytest`.
- When User says not to pipe or select on output, show the full output directly: no file, no `tail`. No exception.
- Output too large for context: redirect to a file (`pytest ... > /tmp/test-output.txt 2>&1`) and read sections, unless User said not to redirect. Limit with `tail -N` on the file afterwards, never on the command.

## User direction supersedes every rule

Every instruction in CLAUDE.md is a default; User's explicit direction overrides it.

- Run a thing exactly the way User said. Do not substitute judgement.
- About to do something User said not to: stop and do what User said.
- No self-persuasion: User said X and a rule says Y, do X and update the rule later.

## pynixd

- A `NIX-DEFECT` is reported on the fork, `Lillecarl/nix`, never upstream.
- Subagent validation: the `checks.*` line only, not the guest suites.

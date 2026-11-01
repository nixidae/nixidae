# lillecarl

## Version control

Use jj, never git, for every version control operation, in every source.

- Finish a task with `jj commit -m "..."`. A fixup for the last commit: `jj squash --use-destination-message`, or `jj squash -m "..."` for a new message.
- Every `jj` command takes `--no-pager`.
- Divide a mixed working copy with `jj split <paths> -m "..."`.
- Finish each task on an empty commit: `jj new` after its last commit, so the next task does not land inside it.
- Git only when User asks for it, or a tool needs git plumbing.
- Subagents never use VCS tools: no `jj`, no `git`. They read and write files and run the gates, and report a failure to the primary agent.

## pynixd

A `NIX-DEFECT` is reported on the fork, `Lillecarl/nix`, never upstream.

## Landing

Landing from a secondary workspace: then advance the default workspace, `~/Code/nixidae`, onto the new `main` (jj skill, `references/workspaces.md`, "Landing a workspace"). croshome builds nixidae from that checkout, not from its lock, so a stale checkout builds stale packages with no error.

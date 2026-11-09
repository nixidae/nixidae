# vivarium-runs

A Claude Code mod. It shows each vivarium run of the session in a pane
beside the chat: what Nix has left to do before the run starts
(`builds 3/10, 1 running · downloads 12/40`, from the plan Nix gave
before building, each store path counted once), each phase's state, the
pause reason, the first line of an error, and the verdict. A paused run
has a Continue button (hotkey `c`), and a live run has a Stop button
(hotkey `s`). The status line names the newest live run, and a toast
says when a run pauses or ends.

The mod follows a run without being told when:

- the vivarium MCP server's `start` tool starts it, or
- a Bash command runs `vivarium run ... --out O` with a literal `O`.

`/vivarium-runs <out>` follows any other run; `/vivarium-runs` alone
opens the pane.

It reads the run only through `vivarium monitor --json --through-pauses`
and sends `vivarium ctl --out O continue|stop`, so it needs no change to
the runner and reaches a run of any vivarium that serves `watch`.

`packages.vivarium` ships it at `share/claude-plugins/vivarium-runs`.
Load it with `claude --plugin-dir <that path>`, or name the path in
`CLAUDE_CODE_PLUGIN_DIRS` in the `env` block of `~/.claude/settings.json`.

Tests: `claude plugin test claude-plugin`. Check:
`claude plugin validate claude-plugin`.

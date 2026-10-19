# Issue migration, 2026-10-19

Every issue of the nine exported repositories moved to nixidae/nixidae
through GraphQL `transferIssue`, oldest first. Issues are then turned off on
those repositories. An old URL redirects, through the web and the REST API.

| file | holds |
| --- | --- |
| `map.jsonl` | one line per issue: `repo`, `old` and `new` number, state, old labels |
| `dump.json` | each issue before transfer: node id, title, type, labels, assignees, parent |
| `commit-refs.jsonl` | each source commit whose message names an issue: bare `#N` and `owner/repo#N` |
| `migrate.py` | the transfer (`--apply`) and the label, type and sub-issue repair (`--fixup`) |
| `snapshot.py` | writes every issue with body and comments as they are now |
| `commit_refs.py` | writes `commit-refs.jsonl` |
| `rewrite_open.py` | rewrites old references in open issues to `#M` (applied 2026-10-19, 225 edits) |
| `rewrite-open.log` | each edited body or comment as it was before that rewrite |

What the transfer did, measured:

- A `#N` in a body or comment became `nixidae/nixidae#M` when issue N had
  moved already, and `nixidae/<source>#N` when it had not. Both resolve.
- Sub-issue links held where both ends moved.
- Labels were dropped. Each issue has `source: <repo>` and an issue type.

What still names old numbers: a bare `#N` in a commit message, which means
`<source>#N`, and `<source>#N` in code comments and docs. `map.jsonl` turns
either into the new number.

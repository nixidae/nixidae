---
name: "Jujutsu Version Control"
description: "Use jujutsu (jj) for all version control operations including commits, branches, merges, rebases, and repository management. Handles git workflow translation to jj equivalents."
---

# Jujutsu Version Control Skill

This skill ensures Claude uses jujutsu (jj) as the primary version control system for all VCS operations in this repository.

## Core Principle

**Prefer jj for all VCS actions; translate git requests to jj.**

When users reference version control operations using git terminology, automatically translate them to jujutsu equivalents. Only fall back to git if jujutsu is unavailable and the user explicitly approves.

## When This Skill Activates

This skill activates when users mention:
- VCS actions: commit, branch, merge, rebase, push, pull, checkout, stash
- Repository state: staged, unstaged, HEAD, detached, conflicts
- Jujutsu-specific terms: bookmarks, workspaces, revsets, operation log, working-copy commit

**Keyword Tripwires**: git, repo, commit, branch, merge, rebase, bookmark, workspace, revset, working-copy

## Key Jujutsu Concepts

### Working Copy as Commit

In jujutsu, the working copy is itself a commit:
- `@` represents the working-copy commit (equivalent to git's "working directory + staging area")
- `@-` represents the parent of the working-copy commit
- Changes are automatically tracked in `@` without explicit staging

### Bookmarks (not Branches)

Jujutsu uses **bookmarks** instead of branches:
- Bookmarks are pointers to commits, managed with `jj bookmark` commands
- **Critical**: Bookmarks do NOT auto-advance when you create new commits
- You must explicitly move bookmarks: `jj bookmark set <name>`
- Bookmarks are local by default; use `--track` for remote tracking

### Workspaces

Workspaces allow multiple working copies of the same repository:
- Similar to git worktrees but more integrated
- Each workspace has its own working-copy commit
- Useful for parallel work on different features

### Git Interoperability

Jujutsu can colocate with git repositories:
- `jj git init` creates a jj repo that tracks a git repo
- `jj git clone` clones a git repository with jj tracking
- In colocated repos, prefer jj for write operations
- `jj git push` and `jj git fetch` sync with git remotes

## Common Command Translations

### Status and Inspection

| Git Command | Jujutsu Equivalent | Notes |
|:------------|:-------------------|:------|
| `git status` | `jj status` | Shows working-copy changes |
| `git log` | `jj log` | Visual graph of commits |
| `git log --oneline` | `jj log -r ::@` | Ancestors of working-copy |
| `git show <commit>` | `jj show <commit>` | Show commit details |
| `git diff` | `jj diff` | Working-copy changes |
| `git diff <commit>` | `jj diff -r <commit>` | Changes in a specific commit |

### Creating and Editing Changes

| Git Command | Jujutsu Equivalent | Notes |
|:------------|:-------------------|:------|
| `git commit -m "msg"` | `jj commit -m "msg"` | Creates new commit, starts new working-copy |
| `git commit --amend` | `jj describe -m "msg"` | Update current working-copy message |
| `git add <file>` | (automatic) | Changes are auto-tracked in `@` |
| `git reset <file>` | `jj restore <file>` | Discard working-copy changes |
| `git commit --fixup` | `jj squash` | Squash working-copy into parent |

### Movement and History Editing

| Git Command | Jujutsu Equivalent | Notes |
|:------------|:-------------------|:------|
| `git checkout <commit>` | `jj edit <commit>` | Edit a commit directly |
| `git rebase -i` | `jj rebase` + `jj squash/split` | More granular operations |
| `git cherry-pick` | `jj duplicate` + `jj rebase` | Copy and move commits |
| `git reset --hard` | `jj abandon @` | Abandon working-copy commit |

### Bookmarks (Branches)

| Git Command | Jujutsu Equivalent | Notes |
|:------------|:-------------------|:------|
| `git branch <name>` | `jj bookmark create <name>` | Create bookmark at current commit |
| `git checkout -b <name>` | `jj bookmark create <name>` + `jj edit @` | Create and stay at commit |
| `git branch -d <name>` | `jj bookmark delete <name>` | Delete bookmark |
| `git checkout <branch>` | `jj new <bookmark>` | Create new working-copy on bookmark |
| `git merge <branch>` | `jj merge <bookmark> @` | Create merge commit |

### Undo Operations

| Git Command | Jujutsu Equivalent | Notes |
|:------------|:-------------------|:------|
| `git reset --hard HEAD~1` | `jj undo` | Undo last operation |
| `git reflog` | `jj op log` | View operation history |
| `git reset --hard <op>` | `jj op restore <op>` | Restore to specific operation |
| `git stash` | `jj new` | Create new working-copy (keeps changes) |
| `git stash pop` | `jj squash` | Squash changes back |

### Remote Operations

| Git Command | Jujutsu Equivalent | Notes |
|:------------|:-------------------|:------|
| `git clone <url>` | `jj git clone <url>` | Clone with git backend |
| `git pull` | `jj git fetch` + `jj rebase` | Fetch and update |
| `git push` | `jj git push` | Push bookmarks to remote |
| `git fetch` | `jj git fetch` | Fetch from remotes |

### File Operations

| Git Command | Jujutsu Equivalent | Notes |
|:------------|:-------------------|:------|
| `git mv <old> <new>` | `jj file move <old> <new>` | Move/rename file |
| `git rm <file>` | `rm <file>` | Just delete the file |
| `git clean -fd` | `jj restore --from @-` | Restore from parent |

## Working Patterns

### Creating a New Change

```bash
# Make changes to files
jj status                    # Review changes
jj commit -m "description"   # Create commit from working-copy
# Working-copy is now a new empty commit on top
```

### Amending the Current Change

```bash
# Make more changes
jj describe -m "updated description"  # Update message
jj squash                             # Squash into parent (alternative)
```

### Creating a Feature Bookmark

```bash
jj bookmark create feature-name    # Create bookmark at @
jj describe -m "feature: ..."      # Describe the change
jj commit -m "feature: part 2"     # Continue work
jj bookmark set feature-name       # Move bookmark to @ (if needed)
```

### Splitting a Change

```bash
jj split                    # Interactively split current commit
# OR
jj split <file1> <file2>   # Split specific files into new commit
```

### Viewing History

```bash
jj log                      # Visual graph of all commits
jj log -r ::@              # Ancestors of working-copy
jj log -r @-               # Parent commit
jj log -r 'bookmarks()'    # All bookmarked commits
```

### Undoing Mistakes

```bash
jj op log                   # View operation history
jj undo                     # Undo last operation
jj op restore <op-id>      # Restore to specific operation
```

## Best Practices for This Repository

1. **Create frequent commits**: Jujutsu makes history editing easy, so commit often with reasonable changesets
2. **Use descriptive commit messages**: Follow the style from `jj log` output
3. **Leverage operation log**: `jj op log` is your safety net - you can always undo
4. **Don't worry about "perfect" commits**: History is mutable and easy to refine
5. **Use `jj bookmark set`**: Remember to move bookmarks when needed (they don't auto-advance)

## Safety Notes

- **Jujutsu does not have a "current branch"**: Bookmarks require explicit management
- **Working-copy is always a commit**: Changes are never lost unless explicitly abandoned
- **Operations are recorded**: `jj op log` tracks every action for easy undo
- **Colocated repos**: In repos with both jj and git, prefer jj for write operations

## Repository-Specific Conventions

Based on the nix-csi repository:
- Use descriptive commit messages (see examples in `jj log`)
- Recent commit message patterns:
  - "Add X to Y" for new features
  - "Make X configurable" for configuration changes
  - "Use X instead of Y" for refactoring
  - "Document X in Y" for documentation
- Commit frequently during development; history can be refined later

## When Jujutsu Is Unavailable

If `jj` is not installed:
1. Inform the user that jujutsu is preferred for this repository
2. Suggest installation: `cargo install jj-cli` or system package manager
3. Only proceed with git commands if the user explicitly approves
4. Remind the user of the benefits of switching to jujutsu

## References

- Jujutsu documentation: https://martinvonz.github.io/jj/
- GitHub repository: https://github.com/martinvonz/jj
- This repository's VCS policy: See CLAUDE.md

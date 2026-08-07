# Contributing to Nixidae projects

Thanks for looking. These are the defaults for every repository in the
[Nixidae](https://github.com/nixidae) organisation. Individual repositories may
ship their own `CONTRIBUTING.md`, and where they do, that one wins.

## Before you write code

For anything more than a typo fix, **open an issue first**. These projects are
pre-1.0 and the shape of things changes; a short conversation up front is
cheaper than a rewrite after review. If an issue already exists, say on it that
you are picking it up.

Small, obviously-correct fixes — typos, broken links, a wrong flag in a doc
example — can go straight to a pull request.

## Setting up

Every repository is a Nix flake, so the development shell is the same everywhere:

```bash
nix develop      # or: direnv allow, if you use direnv
```

That gives you the interpreter, the dependencies and the tooling the CI uses.
You should not need to install anything else, and if you do, that is a bug —
please report it.

Building and running the checks:

```bash
nix build              # the default package
nix flake check        # whatever gates that repo defines
```

Repositories with a `justfile` usually wrap the common tasks; `just --list`
will tell you what is available.

## Making the change

- **Match the surrounding code.** Naming, comment density and idiom vary
  between the Python and the Nix halves of a project — follow whichever you are
  in rather than importing habits from the other.
- **Keep commits focused.** One concern per commit, with a message that says
  what changed and why. A commit that both fixes a bug and reformats a file is
  two commits.
- **Tests are expected** for behaviour changes and bug fixes. If the thing you
  fixed had no test, the pull request is a good place to add one.
- **Type checking is not optional.** The Python projects run `pyright` in
  strict mode; `ruff` handles linting and formatting. Both are in the dev shell.
- **Nix code is formatted** by the repository's formatter — `nix fmt` if the
  flake exposes one.

## Opening the pull request

Fill in the template. The parts that matter most are what the change does and
how you convinced yourself it works — a reviewer who can reproduce your
reasoning reviews much faster than one who cannot.

Draft pull requests are welcome and are a good way to get early feedback on an
approach.

CI must be green before merge. If a check fails for reasons that look unrelated
to your change, say so in a comment rather than force-pushing repeatedly to
find out.

## Reviews

Expect review to be direct and to ask for changes. That is not a verdict on the
contribution; it is how the code gets better. Equally, push back if you think a
review comment is wrong — you may well be right.

Response times vary. Nixidae is maintained by people doing this alongside other
work, so a few days of quiet on a pull request is normal, not a snub. A polite
ping after a week is fine.

## Reporting bugs

Use the issue templates. The single most useful thing you can include is a
reproduction: the exact command, the input, and what happened versus what you
expected. `nix --version`, the operating system, and whether you are on the
flake's `main` or a release matter more often than you would think.

Security issues do **not** go in the issue tracker — see
[SECURITY.md](SECURITY.md).

## Licensing

Contributions are accepted under the licence of the repository you are
contributing to (MIT unless that repository says otherwise). There is no CLA.

## Conduct

Everything here happens under the [Code of Conduct](CODE_OF_CONDUCT.md).

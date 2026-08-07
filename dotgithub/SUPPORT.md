# Getting help

Nixidae projects are maintained in spare time. There is no support contract and
no on-call rota, but questions are welcome and get answered.

## Where to ask

**Read the docs first.** Several projects publish generated documentation:

- [nanopynix](https://lillecarl.github.io/nanopynix/)
- [pynixd](https://lillecarl.github.io/pynixd/)

The other repositories document themselves in their `README.md` and under
`docs/` in the repository itself.

**Then open an issue** on the repository the question is about. Use the
*Question* template if the repository has one, or the blank issue form
otherwise. Questions asked in the open help the next person with the same
problem, which is why there is no private support channel.

Ask on the specific repository rather than here — an issue on
`nixidae/.github` about a `nanopynix` crash will just get moved.

## Making a question easy to answer

The difference between a question answered in an hour and one that sits for a
week is usually detail:

- The exact command you ran and its full output, not a paraphrase
- The version, tag, or flake revision you are on
- Your OS and `nix --version`
- What you expected to happen
- What you already tried

If you can reduce it to a small flake or script that reproduces the problem,
that is worth more than any amount of description.

## What is not support

- **Security problems** go through [SECURITY.md](SECURITY.md), not the issue
  tracker.
- **Bugs in Nix, nixpkgs, or Kubernetes** belong upstream. If you are not sure
  which side a problem is on, ask — working that out is a fair question.
- **"Will you build X for me"** is a feature request, and belongs in an issue
  as one. It may be declined; these are small projects with opinions about
  scope.

## Response times

Days, usually. Sometimes longer when life gets in the way. A follow-up comment
after a week is entirely reasonable and is not nagging.

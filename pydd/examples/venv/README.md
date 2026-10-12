# venv

A Python environment from a lock file. This is the case that pydd is for.

`lock.json` has 24 artefacts, for many platforms. The planner runs
`packaging` on PEP 508 markers and PEP 425 tags, and downloads only the 6
that this target needs. The build log tells why it skips each of the
others, for example `colorama: marker excludes this target`.

Each wheel, sdist and editable project becomes a node. `idna` is an sdist
only, so its build backend, `flit_core`, is a node of the same graph.
`myapp` in `fixtures/` is installed as a PEP 660 editable.

Read `default.nix`, then `src/pydd/python/_resolve.py`. From the top of
the repository:

    nix run --file . run -- examples.venv.cli
    nix run --file . run -- examples.venv.huggorm check

The first lists the environment. `check` runs `check.py` in it:

    idna         3.18 xn--eckwd4c7c.xn--zckzah
    ninja        1.13.0
    editable     /nix/store/cs15iiys…-myapp
      hello from the tree that the check derivation named

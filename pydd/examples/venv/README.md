# venv

A Python environment from a `uv.lock`. This is the case that pydd is for.

`fixtures/myapp` is a uv project, and `uv lock` wrote its `uv.lock`. The
lock has 122 wheels and sdists, for many platforms. The planner walks the
dependency edges from `myapp`, evaluates their markers with `packaging`,
and downloads only the 9 artefacts that this target needs. The build log
tells what it skips and why, for example
`myapp -> colorama: marker excludes this target`.

Each wheel, sdist and editable project becomes a node. `noBinary` makes
`idna` build from its sdist. uv does not lock build backends, so the
`build` group of `pyproject.toml` puts `flit_core` and the installer in
the lock. Both are nodes of the same graph, and neither is installed.
`myapp` itself is installed as a PEP 660 editable.

Read `default.nix`, then `src/pydd/python/_uvlock.py`. From the top of
the repository:

    nix run --file . run -- examples.venv.cli
    nix run --file . run -- examples.venv.huggorm check

The first lists the environment. `check` runs `check.py` in it:

    idna         3.20 xn--eckwd4c7c.xn--zckzah
    ninja        1.13.2
    editable     /nix/store/mp3m0i00…-myapp
      hello from the tree that the check derivation named

To change the dependencies, edit `fixtures/myapp/pyproject.toml` and run
`uv lock` in that folder.

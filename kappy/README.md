# kappy

The apply engine of [easykubenix](https://github.com/nixidae/easykubenix), as a library of its own.
The name plays on [kapp](https://carvel.dev/kapp/), whose job it does.

It depends on [kr8s](https://github.com/kr8s-org/kr8s) and not on Nix.

kappy has no labels or field manager of its own. A caller names the environment label, the unit label and the
field manager, and passes them to each call. `tests/conventions.py` shows one way to bind them once.

- `kappy.apply`: apply a generation with server-side apply, then prune what that generation dropped.
- `kappy.converge`: converge a set of objects. Its steps are injected Protocols, so every decision is testable without an API server.
- `kappy.directapply`: the kr8s `Api` side of `converge`.
- `kappy.testing`: a kr8s `Api` double for tests of code that applies and prunes.

Work happens in [nixidae](https://github.com/nixidae/nixidae), which exports this repository.
Issues go to its tracker, labelled `source: kappy`.

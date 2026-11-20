# modiff

Edit a Nix module system configuration and see what the edit changes.

modiff evaluates a configuration, then records which values the evaluation
needed. An edit adds a module through `extendModules`. modiff evaluates the
same values again and lists every option whose value changed. For each
option it shows the type, the declaration, every module that defines it, and
the value before and after. A value that nothing evaluated shows as
`«thunk»`, and one whose evaluation threw an error as `«failed»`.

An edit that fails to evaluate shows as a failure (`!`) under the option
the error names, or else under the option the edit sets. The detail pane
says where the evaluation met the error first: often `assertions`, which
reads nearly every option.

One value can sit at many paths. Its changes show at the first path, and
each other path shows one row `=  same as <first path>`.

The last row of the cascade lists the derivations the edit rebuilds. Select
one to see why: the inputs that changed and the attributes of its `.drv`
that differ. A derivation added or removed with its own inputs, such as a
service's package with its sources and build tools, shows as one row with
`+N inputs`: the inputs are rows under it. The list compares what the
forced derivations need to build before and after the edit. A package that
an assertion only compares is no rebuild.

It is a tool to learn the module system with: set one option and watch it
reach the firewall, the systemd units and `/etc`.

## Run

```sh
nix run --file . packages.default -- --set services.openssh.enable=true
```

With no arguments modiff loads a minimal NixOS configuration. Other sources:

- `--nixos FILE`: a NixOS configuration module.
- `--expr EXPR --force PATH`: any `evalModules` result. `--force` names what
  "evaluated" means, such as `config.system.build.toplevel.drvPath`.

## Keys

| key | does |
| --- | --- |
| `/` | find any option by words of its path, and show it first in the cascade |
| `a` | add an edit: an option path (tab completes) and a Nix expression |
| `e` | edit the option under the cursor |
| `space` | turn the selected edit on or off |
| `x` | delete the selected edit |
| `w` | write the edits that are on to a file, as a module to import |
| `D` | show every definition of an option; by default the ones the edits left alone fold after three |
| `tab` | move between the edits, the cascade and the detail pane |
| `pgup` `pgdn` | scroll the detail pane, once `tab` has moved to it |
| `↑` `↓` | move the cursor; the detail pane follows it |
| `enter` | open or close a row; on a changed input of a derivation, go to its row |
| `escape` | close a dialog |
| `?` | show the keys and what each mark in the cascade means |
| `q` | quit |

Inside an edit, `lib`, `config` and the module arguments (`pkgs` on NixOS)
are in scope: `lib.mkForce true` works.

## Develop

```sh
nix-shell --run pytest
nix-shell --run 'ruff check && ruff format --check && zuban mypy'
```

The same in the Nix sandbox, from the files the package tracks:

```sh
nix build --file . checks.pytest checks.lint checks.format checks.types
```

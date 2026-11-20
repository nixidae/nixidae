# modiff

Edit a Nix module system configuration and see what the edit changes.

modiff evaluates a configuration, then records which values the evaluation
needed. An edit adds a module through `extendModules`. modiff evaluates the
same values again and lists every option whose value changed. For each
option it shows the type, the declaration, every module that defines it, and
the value before and after. A value that nothing evaluated shows as
`«thunk»`.

The last row of the cascade lists the derivations the edit rebuilds. Select
one to see why: the inputs that changed and the attributes of its `.drv`
that differ.

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
| `tab` | move between the edits and the cascade |
| `enter` | open or close a row; on a changed input of a derivation, go to its row |
| `?` | show the keys and what each mark in the cascade means |
| `q` | quit |

Inside an edit, `lib`, `config` and the module arguments (`pkgs` on NixOS)
are in scope: `lib.mkForce true` works.

## Develop

```sh
nix-shell --run pytest
nix-shell --run 'ruff check && ruff format --check && zuban mypy'
```

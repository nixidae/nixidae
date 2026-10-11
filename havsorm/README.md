# havsorm

Salt states written in Nix. havsorm is a Salt extension that adds a
`#!nix` renderer. huggorm evaluates the SLS file in the Salt process, so
a minion needs no Nix daemon and no `nix` command.

## An SLS file in Nix

The file is an attribute set of states:

    #!nix
    {
      hello."pkg.installed" = [ { name = "hello"; } ];
    }

Or a function that takes what Salt knows about the minion:

    #!nix
    { grains, pillar, saltenv, sls, ... }:
    {
      "motd-${grains.id}"."file.managed" = [
        { name = "/etc/motd"; contents = "role: ${pillar.role}\n"; }
      ];
    }

A Nix attribute set has no order, so Salt sees its states sorted by
name. To keep the order you wrote, use a list of sets with one state
each:

    #!nix
    [
      { first."test.nop" = [ ]; }
      { second."test.nop" = [ ]; }
    ]

A function must name its arguments. `x: ...` is refused.

A Nix error is a Salt render error. Its position reads `«string»:line:column`,
and the line is the line in the SLS file.

## Files beside the SLS

A relative path names a file on Salt's fileserver, in the SLS's
saltenv:

    #!nix
    let ports = import ./ports.nix; in
    { web."test.nop" = [ { port = ports.http; } ]; }

The evaluator reads the saltenv through the render's file client: in a
state render that serves file_roots, and in a pillar SLS pillar_roots.
It lists the files once per render, and fetches a file the first time
Nix reads it. The tree appears in Nix at a store path named
`salt-<saltenv>`, which no store holds.

## Pillar from a Nix file

On the master:

    ext_pillar:
      - nix: /srv/nix-pillar/default.nix

The file is an attribute set, or a function that takes
`{ id, grains, pillar, ... }`:

    { id, grains, pillar, ... }:
    let roles = import ./roles.nix; in
    {
      role = roles.${id} or "default";
      ntp.servers = if grains.os == "Leap" then [ "0.opensuse.pool.ntp.org" ] else [ ];
    }

`pillar` holds what the earlier pillar sources gave. A relative path
names a file beside this one, on the master's disk. The dictionary form
also takes `store`:

    ext_pillar:
      - nix:
          file: /srv/nix-pillar/default.nix
          store: dummy://

## Reactor SLS files

A reactor SLS can be `#!nix` too. The function also gets the event's
`tag` and `data`:

    #!nix
    { tag, data, ... }:
    {
      "sync-${data.id}"."local.saltutil.sync_grains" = [ { tgt = data.id; } ];
    }

The reactor renders one event at a time, and each render makes a new
evaluator. Keep a reaction small, and start an orchestration for
anything more.

## Evaluating on a minion

    salt '*' havsorm.eval 'builtins.currentSystem'
    salt '*' havsorm.eval '{ grains, ... }: grains.os'

The module is `havsorm`, because Salt's own `nix` module drives the
`nix-env` command.

## Nix store states

    hello:
      havsorm.realised:
        - name: /nix/store/...-hello-2.12.1
        - root: /nix/var/nix/gcroots/salt/hello

`havsorm.realised` substitutes a store path that the store does not
hold, and roots it so the collector keeps it. It never builds: a
minion takes what a builder made. It reports what it changed, and
`test=True` changes nothing.

A profile points at a store path through numbered generations, as Nix
keeps them:

    salt-env:
      havsorm.profile:
        - name: /nix/var/nix/profiles/salt
        - path: /nix/store/...-salt-env

    salt-env-history:
      havsorm.generations_deleted:
        - name: /nix/var/nix/profiles/salt
        - older_than: 30d

`havsorm.profile` substitutes the path if needed and makes a new
generation only on a change, so the old one stays for a rollback.
`havsorm.generations_deleted` deletes the generations but the current
one, or those older than `older_than`, by Nix's own rules.

A systemd unit from the store, restarted the way Salt restarts any
service whose unit file changed:

    nginx-unit:
      havsorm.unit:
        - name: nginx.service
        - path: /nix/store/...-unit-nginx.service/nginx.service
    nginx:
      service.running:
        - watch:
          - havsorm: nginx-unit

`havsorm.unit` substitutes the store path that holds the unit, roots it
at `/nix/var/nix/gcroots/salt/<name>` so the collector keeps the
service's closure, links the unit into `/etc/systemd/system`, and
reloads systemd when the link changed. It never replaces a unit file
that is not its own link.

A NixOS host, switched to a system a builder made, as `nixos-rebuild
switch` switches it:

    web:
      havsorm.system:
        - name: /nix/store/...-nixos-system-web1-26.05

The minion substitutes the system's closure, makes a new generation of
`/nix/var/nix/profiles/system`, and runs `switch-to-configuration` under
`systemd-run`. `action: boot` and `action: test` work as in
`nixos-rebuild`. `test=True` runs `dry-activate`, which lists the units
the switch would restart.

`havsorm.rolled_back` goes back one generation and activates it, for an
orchestration's `onfail`. `salt web1 havsorm.system_rollback` does the
same from the command line.

Garbage collection, as a condition:

    nix-store:
      havsorm.garbage_collected:
        - keep_free: 10G

With `keep_free`, it collects only when the store's file system has
less free than that. It deletes only what no root reaches, so
profiles, units and realised paths survive. `test=True` lists the dead
paths through Nix's own `--print-dead` answer.

Nix itself, with no installer script and no distribution package:

    nix:
      havsorm.store_present:
        - version: 2.34.6
        - settings:
            experimental-features: nix-command flakes
            build-users-group: nixbld

The minion reads Nix's release `fallback-paths.nix` for its system,
opens the store (which creates `/nix`), substitutes Nix's closure,
points `/nix/var/nix/profiles/default` at it, writes `nix.conf`, and
links `nix-daemon.socket` and `nix-daemon.service` from Nix's own
units. Without `version` it installs the Nix that havsorm links. Build
users are Salt's `group.present` and `user.present`.

## Configuration

    havsorm:
      eval_store: dummy://
      store: auto
      nix_settings:
        substituters: https://cache.nixos.org

`eval_store` is the store an evaluation uses. The default, `dummy://`,
holds nothing, so a file that reads a derivation or copies a path
fails. `store` is the store the states change. `nix_settings` are
nix.conf settings for the job's process.

## Installing

    salt-pip install havsorm

Salt finds the renderer through the `salt.loader` entry point. This
needs huggorm on PyPI, which is not published yet. Until then, build
with Nix:

    nix build --file . havsorm

## NixOS

`nix/nixos.nix` runs nixpkgs' `services.salt.master` and
`services.salt.minion` from havsorm's `saltEnv`, a Python with Salt and
havsorm:

    imports = [ havsorm.nixosModule ];
    services.havsorm.package = havsorm.saltEnv;
    services.salt.minion.enable = true;

A switch does not restart the minion, because `havsorm.system` runs
inside one of the minion's jobs, and a restart would lose its return.

`nix build --file . fleet` boots a master and a NixOS minion on QEMU and
an openSUSE minion on UML, with vivarium. The master applies a `#!nix`
state to both, switches the NixOS minion to another system and back, and
deploys it again from an event through a `#!nix` reactor.

## Uyuni and SUSE Multi-Linux Manager

A client renders its states in the Salt bundle, `venv-salt-minion`:
Salt 3006.0 on Python 3.11. Install havsorm into it the way the bundle
documents for any package:

    salt <minion> pip.install havsorm

A formula or a state channel can then hold `#!nix` SLS files and the
`.nix` files they import. `nix build --file . bundle` runs the bundle's
own `salt-call` on such files.

`formulas/` holds `nix-host`, a formula in Uyuni's layout: copy
`formulas/metadata/nix-host` to `/srv/formula_metadata/nix-host` and
`formulas/states/nix-host` to `/srv/salt/nix-host`. Its form asks for
a Nix version, the experimental features, an optional profile and a
free space floor, and its `#!nix` state installs Nix, sets the profile
and collects garbage from that pillar.

## Limits

- Each render makes a new evaluator, and collects after it.

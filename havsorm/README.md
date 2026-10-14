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

## Calling Salt from Nix

A function that takes `salt` can call any execution function, as
Jinja's `salt['...']` does. A list is positional arguments, a set is
keyword arguments:

    #!nix
    { salt, ... }:
    let
      webs = salt "mine.get" { tgt = "roles:web"; tgt_type = "grain"; fun = "lab_ip"; };
    in
    { lb."file.managed" = [ { name = "/etc/backends"; }
                           { contents = builtins.toJSON webs; } ]; }

The call runs when the evaluation reaches it, and its answer crosses as
JSON. The ext_pillar gets `salt` too, with the master's functions.

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

The reactor renders one event at a time, in one process, so its
evaluator stays warm: the files it evaluated stay evaluated from one
event to the next. A file that holds `import <nixpkgs> { }` gave
`hello.drvPath` in 205 ms fresh and 9 ms warm. Before each render, the
evaluator checks every file it holds: a fileserver file by the content
the fileserver serves now, a file on disk by its mtime and size. On any
change it evaluates everything again. A reaction still blocks the next
event, so start an orchestration for anything long.

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

## Grains

    salt -G 'nixos:system:/nix/store/...-nixos-system-web1-26.05' test.ping
    salt -G 'nix:daemon:True' havsorm.collect_garbage

`nix` holds the Nix havsorm links (`version`, `system`), whether the host
has a store and a daemon, and the default profile's target. `nixos`
holds a NixOS host's running, booted and next-boot systems and its
version. `havsorm.system` refreshes the grains after a switch.

## Configuration

    havsorm:
      eval_store: dummy://
      warm: false
      store: auto
      nix_settings:
        substituters: https://cache.nixos.org

`eval_store` is the store an evaluation uses. The default, `dummy://`,
holds nothing, so a file that reads a derivation or copies a path
fails. `warm: true` keeps a warm evaluator, as the reactor does, in
every process that renders `#!nix` or reads the Nix ext_pillar: on a
master, the workers that compile pillar. A minion job runs in a process
of its own, so it gains nothing there. `store` is the store the states
change. `nix_settings` are
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

`nix build --file . fleet` runs a fleet on vivarium: a master and two
NixOS web minions on QEMU, an openSUSE web minion and a NixOS load
balancer on UML. The master serves a signed binary cache, and every
deploy substitutes from it. Each phase is one scenario:

- `bootstrap`: salt-ssh installs the openSUSE minion with a `#!nix`
  state, as Uyuni bootstraps a client.
- `nix-on-suse`: `havsorm.store_present` installs Nix on openSUSE and
  starts its daemon.
- `nixos`: a switch, a grain target, a rollback, and a deploy from an
  event through a `#!nix` reactor.
- `rolling`: the orchestration below.
- `service`: `havsorm.unit` runs one Nix-built web server on NixOS and
  openSUSE. A load balancer builds its haproxy config in Nix from the
  mine, with `salt "mine.get"`, and stops sending to a minion that leaves
  the mine.
- `heal`: a service beacon sees the web server stop on openSUSE, and a
  `#!nix` reaction starts it again.

`tests/fleet/cache-key` is a test key: its secret is in the repository.

### A rolling deploy

`tests/fleet/srv/orch/deploy.sls` is an orchestration in Nix:

    salt-run state.orchestrate orch.deploy \
      pillar='{systems: {web1: /nix/store/...}, good: {web1: /nix/store/...}}'

It deploys one minion at a time, and each step requires the one before,
so a failure stops the rest. On a failure, every minion goes back to its
system in `good`. The fleet test deploys a system whose activation
fails: web1 gets it, web2 never does, and both run the good system
after.

Salt's `batch` with `failhard` does not do this. Salt also reads that
`failhard` as the orchestration's own, so the run ends before the
restore.

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

- Outside the reactor and `warm: true`, each render makes a new
  evaluator. Either way it collects after each render.

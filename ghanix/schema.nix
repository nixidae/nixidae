# NixOS-module-system schema for GitHub Actions workflows. Deliberately
# generic -- nothing here is nanopynix-specific, so this file could be
# dropped into any other Nix project rendering its own CI with the same
# toYAML-based approach.
#
# Jobs are naturally a YAML mapping (keyed by job id, order irrelevant --
# execution order comes from `needs:`, not declaration order), so `jobs` is
# `attrsOf submodule`. Steps are naturally an ordered YAML sequence, so
# `steps` is `listOf submodule` -- a plain Nix list is already ordered, so
# there's no need for an artificial "order" field the way an attrsOf-keyed
# collection of steps would need one.
#
# Every GHA key used here (`runs-on`, `"if"`, `"with"`) is used verbatim as
# the option name: hyphenated and keyword-clashing attribute names both work
# fine as Nix attribute names (quoted for keywords), so no separate
# Nix-name/YAML-name translation layer is needed.
{ lib }:
let
  inherit (lib) mkOption types;

  inherit (import ./steps.nix { inherit lib; }) steps;

  /*
    Where each generated step lands in a job's `steps`.

    `types.listOf` concatenates its definitions in order of priority, so a
    step contributed at a number below 1000 comes before every step the
    caller wrote -- 1000 is what a plain list definition gets. The numbers
    are spaced so that a step added later can go between two of these
    without renumbering the rest.

    The order itself is not arbitrary. A checkout has to come first because
    everything after it reads the tree. Room is made before Nix installs,
    because the installer writes to the disk being cleared. The runner's
    own permissions come last of the generated steps, so that a job which
    cannot install Nix fails saying so rather than saying `sysctl`.
  */
  orders = {
    checkout = 100;
    freeDiskSpace = 200;
    installNix = 300;
    cachix = 400;
    userNamespaces = 500;
    openKvm = 600;
  };

  /*
    The steps a job asks for by name rather than by writing them out.

    Everything here was copied between repositories before it was an
    option, and the copies drifted: the user-namespace step existed in
    three places with three bodies, and the job that needed it most did not
    have it at all.

    This whole set lives under one attribute on purpose. `evalWorkflow`
    strips exactly `ghanix` from each job before rendering, because a job
    is freeform -- every other key it carries goes to GitHub verbatim, and
    a key GitHub does not know makes it reject the workflow at load, long
    after any check here has passed.
  */
  runnerModule = {
    options = {
      checkout = {
        enable = mkOption {
          type = types.bool;
          default = false;
          description = "Check the repository out before anything else runs.";
        };
        ref = mkOption {
          type = types.nullOr types.str;
          default = null;
          description = "Ref to check out. GitHub's default is the ref that triggered the run.";
        };
        fetchDepth = mkOption {
          type = types.nullOr types.int;
          default = null;
          description = ''
            Commits to fetch. The default checkout is a single commit, so a
            job that reads a range of them needs `0`, meaning all of them.
          '';
        };
        timeoutMinutes = mkOption {
          type = types.int;
          default = 10;
          description = "Cap on the checkout step.";
        };
      };

      freeDiskSpace = {
        enable = mkOption {
          type = types.bool;
          default = false;
          description = ''
            Delete the toolchains the runner image ships that no job here
            uses. For a closure that does not fit in the 25 GiB a runner
            starts with.
          '';
        };
        timeoutMinutes = mkOption {
          type = types.int;
          default = 10;
          description = "Cap on the step.";
        };
      };

      nix = {
        install = {
          enable = mkOption {
            type = types.bool;
            default = false;
            description = "Install Nix with `cachix/install-nix-action`.";
          };
          experimentalFeatures = mkOption {
            type = types.listOf types.str;
            default = [
              "nix-command"
              "flakes"
            ];
            description = "What the installed Nix has to allow.";
          };
          settings = mkOption {
            type = types.attrsOf (
              types.oneOf [
                types.bool
                types.int
                types.str
                (types.listOf types.str)
              ]
            );
            default = { };
            example = {
              trusted-users = [
                "root"
                "runner"
              ];
              max-jobs = 1;
            };
            description = ''
              Everything else the installed Nix should hold, as nix.conf
              keys. A list joins on spaces.

              `substituters` and `trusted-public-keys` belong here, and so
              does `trusted-users`: a daemon ignores a substituter that a
              user it does not trust asks for, so a project with a cache of
              its own needs all three or it gets none of them.
            '';
          };
          timeoutMinutes = mkOption {
            type = types.int;
            default = 15;
            description = "Cap on the install step.";
          };
        };

        cachix = {
          enable = mkOption {
            type = types.bool;
            default = false;
            description = ''
              Read from, and on a push write to, a cachix cache.

              Reading is unauthenticated, so a fork's pull request still
              gets the cache; only a run holding the secret writes.
            '';
          };
          name = mkOption {
            type = types.str;
            default = "lillecarl";
            description = "The cache.";
          };
          useDaemon = mkOption {
            type = types.bool;
            default = false;
            description = "Push through cachix's daemon rather than at the end of the job.";
          };
          pushFilter = mkOption {
            type = types.nullOr types.str;
            default = null;
            example = "(-uml-test-)";
            description = ''
              A regular expression naming store paths *not* to push.

              For an output whose existence is the result of a test rather
              than a thing to reuse: push it, and the next run with the
              same inputs substitutes the answer instead of running the
              test.
            '';
          };
          skipPush = mkOption {
            type = types.nullOr types.bool;
            default = null;
            description = "Read only, never write.";
          };
          timeoutMinutes = mkOption {
            type = types.int;
            default = 15;
            description = "Cap on the cachix step.";
          };
        };
      };

      userNamespaces = {
        enable = mkOption {
          type = types.bool;
          default = false;
          description = ''
            Let the runner open an unprivileged user namespace, which
            Ubuntu denies by default.

            Needed by the Nix sandbox, and by passt -- which unshares one
            before it serves a guest's uplink and exits if it cannot. So
            any job that boots a guest needs this, sandboxed or not.
          '';
        };
        timeoutMinutes = mkOption {
          type = types.int;
          default = 5;
          description = "Cap on the step.";
        };
      };

      openKvm = {
        enable = mkOption {
          type = types.bool;
          default = false;
          description = ''
            Let the runner user open /dev/kvm. x64 runners only; GitHub's
            ARM runners have no such device.
          '';
        };
        timeoutMinutes = mkOption {
          type = types.int;
          default = 5;
          description = "Cap on the step.";
        };
      };

      deriveTimeout = {
        enable = mkOption {
          type = types.bool;
          default = false;
          description = ''
            Set the job's `timeout-minutes` from the caps of its steps.

            GitHub applies both caps and the smaller wins, so a job cap is
            not a second opinion about how long the work takes: it is a
            backstop for the time that belongs to no step. Derive it, and a
            raised step cap cannot leave a job cap behind that silently
            overrides it.

            The sum is a sum of worst cases, so the result is much larger
            than any run. That is correct for a backstop.

            An explicit `timeout-minutes` on the job still wins.

            This has to live here rather than in a caller, because a caller
            cannot see the steps these options contribute -- it holds only
            the ones it wrote. A sum taken before evaluation comes out
            short by exactly the steps it could not see.
          '';
        };
        slack = mkOption {
          type = types.int;
          default = 15;
          description = ''
            Minutes to add for the time no step covers.

            The post phase of an action is the real case:
            `cachix/cachix-action` pushes what the job built after the last
            step ends, and no step cap reaches it.
          '';
        };
      };
    };
  };

  stepModule = { ... }: {
    # Action `with:` shapes vary per-action and aren't worth modeling
    # exhaustively; freeform passthrough covers anything not listed below
    # (e.g. rare per-step keys like `shell` or `continue-on-error`).
    freeformType = types.attrsOf types.anything;
    options = {
      name = mkOption {
        type = types.nullOr types.str;
        default = null;
        description = ''
          Display name for the step in the Actions UI. Steps without one
          show their `run`/`uses` command instead.
        '';
      };
      id = mkOption {
        type = types.nullOr types.str;
        default = null;
        description = ''
          Step id, so later steps or job `outputs` can reference this
          step's outputs as `steps.<id>.outputs.*`.
        '';
      };
      uses = mkOption {
        type = types.nullOr types.str;
        default = null;
        description = "Action to run, as `owner/repo@ref`. Mutually exclusive with `run` in practice.";
      };
      run = mkOption {
        type = types.nullOr types.lines;
        default = null;
        description = "Shell command(s) to run. Mutually exclusive with `uses` in practice.";
      };
      "if" = mkOption {
        type = types.nullOr types.str;
        default = null;
        description = "GitHub Actions expression gating whether this step runs, e.g. `\${{ !cancelled() }}`.";
      };
      "with" = mkOption {
        type = types.nullOr (types.attrsOf types.anything);
        default = null;
        example = {
          ref = "main";
        };
        description = "Inputs passed to the action named by `uses`. Shape varies per action, so this is intentionally untyped.";
      };
      env = mkOption {
        type = types.nullOr (types.attrsOf types.str);
        default = null;
        description = "Environment variables scoped to this step.";
      };
      timeout-minutes = mkOption {
        type = types.nullOr types.int;
        default = null;
        description = ''
          Minutes GitHub waits before killing this step.

          A job-level cap answers "the job stopped". A step-level cap answers
          "which part of the job stopped", and it lets a long step sit beside
          a short one without giving the short one the slack of the long one:
          a 40-minute build and a 10-minute test suite under one 50-minute job
          cap let a hung suite run for 40 minutes.
        '';
      };
    };
  };

  jobModule =
    { config, ... }:
    {
    freeformType = types.attrsOf types.anything;
    options = {
      ghanix = mkOption {
        type = types.submodule runnerModule;
        default = { };
        description = ''
          Steps this job asks for by name. Each one it enables is put at
          the front of `steps`, in the order `orders` above gives, before
          every step the job wrote for itself.

          Stripped before rendering: nothing under here reaches GitHub.
        '';
      };

      runs-on = mkOption {
        type = types.either types.str (types.listOf types.str);
        default = "ubuntu-24.04";
        description = "Runner label(s) this job executes on.";
      };
      timeout-minutes = mkOption {
        type = types.nullOr types.int;
        default = null;
        description = ''
          Minutes GitHub waits before killing this job. Left unset, GitHub's
          default is six hours -- long enough that a job which hangs rather
          than fails burns most of a day of runner time before anyone sees a
          result, and reports nothing when it finally stops.
        '';
      };
      needs = mkOption {
        type = types.nullOr (types.either types.str (types.listOf types.str));
        default = null;
        description = "Job id(s) this job depends on; GitHub Actions waits for them and exposes their outputs.";
      };
      "if" = mkOption {
        type = types.nullOr types.str;
        default = null;
        description = "GitHub Actions expression gating whether this job runs.";
      };
      permissions = mkOption {
        type = types.nullOr (types.attrsOf types.str);
        default = null;
        example = {
          contents = "write";
        };
        description = "`GITHUB_TOKEN` permission scopes granted to this job.";
      };
      environment = mkOption {
        type = types.nullOr (types.attrsOf types.anything);
        default = null;
        example = {
          name = "github-pages";
        };
        description = "Deployment environment this job targets.";
      };
      concurrency = mkOption {
        type = types.nullOr (types.attrsOf types.anything);
        default = null;
        example = {
          group = "pages";
          cancel-in-progress = false;
        };
        description = "Concurrency group for this job, to serialize or cancel overlapping runs.";
      };
      strategy = mkOption {
        type = types.nullOr (types.attrsOf types.anything);
        default = null;
        example = {
          fail-fast = false;
          matrix.version = [
            "a"
            "b"
          ];
        };
        description = "Matrix/fail-fast strategy fanning this job out across multiple runs.";
      };
      outputs = mkOption {
        type = types.nullOr (types.attrsOf types.str);
        default = null;
        description = "Named outputs this job exposes to jobs that `need` it, as GitHub Actions expression strings.";
      };
      env = mkOption {
        type = types.nullOr (types.attrsOf types.str);
        default = null;
        description = ''
          Environment variables shared by every step of this job.

          This is where a workflow expression belongs. A value that reaches a
          step through `env` cannot become part of a command, and it keeps the
          `run` body free of `''${{ ... }}` -- which is what lets that body be
          a single line that calls a script.
        '';
      };
      steps = mkOption {
        type = types.listOf (types.submodule stepModule);
        default = [ ];
        description = "Ordered steps this job runs.";
      };
    };

    # The job's own cap, summed from the steps it ends up with -- the ones
    # `config.steps` below contributes included, which is the whole reason
    # this is here and not in a caller.
    #
    # `mkDefault`, so a job that states its own cap keeps it.
    config.timeout-minutes = lib.mkIf config.ghanix.deriveTimeout.enable (
      lib.mkDefault (
        lib.foldl' (
          total: step:
          total
          + (
            if step.timeout-minutes != null then
              step.timeout-minutes
            else
              throw ''
                ghanix: a step of this job declares no timeout-minutes, so
                `ghanix.deriveTimeout` cannot sum the job's cap. Give the
                step one, or set the job's timeout-minutes itself. The step
                was:
                ${builtins.toJSON step}
              ''
          )
        ) config.ghanix.deriveTimeout.slack config.steps
      )
    );

    # `lib.mkOrder` with an empty list rather than `lib.mkIf`: a definition
    # that contributes nothing is simpler than one that is not there, and
    # it keeps the order visible beside the condition.
    config.steps =
      let
        cfg = config.ghanix;
      in
      lib.mkMerge [
        (lib.mkOrder orders.checkout (
          lib.optional cfg.checkout.enable (steps.checkout { inherit (cfg.checkout) ref fetchDepth timeoutMinutes; })
        ))
        (lib.mkOrder orders.freeDiskSpace (
          lib.optional cfg.freeDiskSpace.enable (
            steps.freeDiskSpace { inherit (cfg.freeDiskSpace) timeoutMinutes; }
          )
        ))
        (lib.mkOrder orders.installNix (
          lib.optional cfg.nix.install.enable (
            steps.installNix { inherit (cfg.nix.install) experimentalFeatures settings timeoutMinutes; }
          )
        ))
        (lib.mkOrder orders.cachix (
          lib.optional cfg.nix.cachix.enable (
            steps.cachix { inherit (cfg.nix.cachix) name useDaemon pushFilter skipPush timeoutMinutes; }
          )
        ))
        (lib.mkOrder orders.userNamespaces (
          lib.optional cfg.userNamespaces.enable (steps.userNamespaces { inherit (cfg.userNamespaces) timeoutMinutes; })
        ))
        (lib.mkOrder orders.openKvm (
          lib.optional cfg.openKvm.enable (steps.openKvm { inherit (cfg.openKvm) timeoutMinutes; })
        ))
      ];
  };

  workflowModule = {
    options = {
      name = mkOption {
        type = types.nullOr types.str;
        default = null;
        description = ''
          Workflow display name, shown in the Actions UI.

          Optional, because GitHub's is: a workflow with no `name` is shown
          by its path instead. Adding one to a workflow that had none is not
          a cosmetic change -- `''${{ github.workflow }}` is that name, and a
          `concurrency` group built from it becomes a different group.
        '';
      };
      on = mkOption {
        type = types.attrsOf types.anything;
        description = ''
          Trigger configuration, passed through verbatim -- shape varies
          too widely across trigger types to usefully type.
        '';
      };
      env = mkOption {
        type = types.nullOr (types.attrsOf types.str);
        default = null;
        description = ''
          Environment variables shared by every job of this workflow.

          A setting that every job needs belongs here rather than in each job,
          so that a job added later cannot be the one that forgets it.
        '';
      };
      permissions = mkOption {
        type = types.nullOr (types.attrsOf types.str);
        default = null;
        example = {
          contents = "read";
        };
        description = ''
          `GITHUB_TOKEN` permission scopes for every job of this workflow.

          A job that sets its own `permissions` replaces this rather than
          adding to it, which is GitHub's rule and not this schema's.
        '';
      };
      concurrency = mkOption {
        type = types.nullOr (types.attrsOf types.anything);
        default = null;
        example = {
          group = "\${{ github.workflow }}-\${{ github.ref }}";
          cancel-in-progress = true;
        };
        description = ''
          Concurrency group for the whole workflow.

          `cancel-in-progress` here cancels a run that is still going when a
          new one starts, which is usually right for a branch and usually
          wrong for a tag: a cancelled run can stop halfway through
          publishing.
        '';
      };
      jobs = mkOption {
        type = types.attrsOf (types.submodule jobModule);
        default = { };
        description = ''
          Workflow jobs, keyed by job id. Order doesn't matter -- GitHub
          Actions schedules jobs from the `needs:` dependency graph, not
          declaration order.
        '';
      };
    };
  };

  # Recursively drops unset (null-default) option values so the rendered
  # YAML only contains keys a job/step actually set. The same trick
  # nixpkgs' own systemd module uses to turn unit-file option sets into INI
  # sections (`lib.filterAttrs (_: v: v != null)` in systemd/lib.nix's
  # attrsToSection) -- just applied recursively, since steps nest a list of
  # attrsets rather than a single flat one.
  #
  # Scoped to `jobs` only -- `on` is rendered verbatim, since a trigger
  # block can contain a deliberate `null` (e.g. a bare `workflow_dispatch`
  # with no inputs), which isn't an unset option.
  stripNulls =
    value:
    if builtins.isAttrs value then
      lib.mapAttrs (_: stripNulls) (lib.filterAttrs (_: v: v != null) value)
    else if builtins.isList value then
      map stripNulls value
    else
      value;

  # NixOS's own assertions/warnings option shape (nixos/modules/misc/
  # assertions.nix), vendored inline rather than imported from a nixpkgs
  # checkout path -- ghanix only ever needs `lib`, not a nixpkgs source
  # tree on disk. `lib.asserts.checkAssertWarn` below (used to check this
  # pair) is real, un-vendored nixpkgs library code; only this small
  # option-declaration pair is copied.
  assertionsModule = {
    options = {
      assertions = mkOption {
        type = types.listOf types.unspecified;
        internal = true;
        default = [ ];
        description = ''
          Conditions that must hold for workflow evaluation to succeed,
          along with the error messages to show when they don't.
        '';
      };
      warnings = mkOption {
        type = types.listOf types.str;
        internal = true;
        default = [ ];
        description = "Warnings to show during workflow evaluation.";
      };
    };
  };

  # A job with no steps is always an authoring mistake (a builder forgot to
  # set `steps`, or a conditional list ended up empty) -- expressed as a
  # module contributing to `config.assertions` from `config.jobs`, the same
  # pattern any real NixOS module uses to validate one option against
  # another.
  noEmptyJobsModule =
    { config, ... }:
    {
      assertions = [
        {
          assertion = lib.all (job: job.steps != [ ]) (lib.attrValues config.jobs);
          message =
            let
              empty = lib.attrNames (lib.filterAttrs (_: job: job.steps == [ ]) config.jobs);
              who = if config.name == null then "unnamed workflow" else "workflow '${config.name}'";
            in
            "${who}: job(s) with no steps: ${lib.concatStringsSep ", " empty}";
        }
      ];
    };
in
{
  evalWorkflow =
    {
      on,
      jobs,
      name ? null,
      env ? null,
      permissions ? null,
      concurrency ? null,
    }:
    let
      evaluated = lib.evalModules {
        modules = [
          assertionsModule
          workflowModule
          noEmptyJobsModule
          {
            inherit
              name
              on
              jobs
              env
              permissions
              concurrency
              ;
          }
        ];
      };
      cfg = evaluated.config;
    in
    lib.asserts.checkAssertWarn cfg.assertions cfg.warnings (
      {
        inherit (cfg) on;
      }
      // lib.optionalAttrs (cfg.name != null) { inherit (cfg) name; }
      // lib.optionalAttrs (cfg.env != null) { inherit (cfg) env; }
      // lib.optionalAttrs (cfg.permissions != null) { inherit (cfg) permissions; }
      // lib.optionalAttrs (cfg.concurrency != null) { inherit (cfg) concurrency; }
      // {
        /*
          `ghanix` is this schema's own, and GitHub has never heard of it.

          A job is freeform, so every other key it carries is rendered
          verbatim -- which is the point, and which is also why this line
          matters. A key GitHub does not know makes it refuse to load the
          whole workflow, and no check here would catch that: a render gate
          compares the file against this evaluation, so both sides would
          agree on a file that no runner will read. The failure arrives as
          every job of the repository disappearing.

          One key, and one line. That is why everything a job asks for by
          name lives under it.
        */
        jobs = stripNulls (lib.mapAttrs (_: job: removeAttrs job [ "ghanix" ]) cfg.jobs);
      }
    );
}

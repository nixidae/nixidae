# Minimal function+attrset library of GitHub Actions step constructors.
# Workflow- and job-level shape (defaults, option types, merging) is handled
# by schema.nix's lib.evalModules-based schema; this file only builds the
# plain step/list values that get fed into it.
{ lib }:
rec {
  # `if` is a Nix keyword, so it can't be a formal-argument name -- callers
  # pass the condition as `cond` and this helper renders it under the
  # literal (quoted) attribute name "if". Works for both step attrs and
  # job attrs.
  withCond = cond: attrs: if cond == null then attrs else attrs // { "if" = cond; };

  # Put a cap on one step. `schema.nix` gives the reason a step carries its
  # own cap beside the cap of the job.
  withTimeout = minutes: attrs: attrs // { timeout-minutes = minutes; };

  # What a runner image ships that a Nix job never uses -- the toolchains
  # and the browsers both, as `jlumbroso/free-disk-space` lists them.
  #
  # No option to keep any of it. A job here builds Nix derivations; it does
  # not drive a browser or run dotnet, and a knob nobody would turn is a
  # knob that only makes this harder to read.
  removable = [
    "/usr/lib/jvm"
    "/usr/share/dotnet"
    "/usr/share/swift"
    "/usr/local/.ghcup"
    "/usr/local/julia*"
    "/usr/local/lib/android"
    "/opt/az"
    "/usr/local/share/powershell"
    "/opt/hostedtoolcache"
    "/usr/local/share/chromium"
    "/opt/microsoft"
    "/opt/google"
    "/usr/lib/firefox"
  ];

  # An attrset of nix.conf settings as the lines `extra_nix_config` wants.
  # A list joins on spaces, which is how nix.conf spells every plural
  # setting it has; a bool is `true`/`false` and never `1`/`0`.
  nixConf =
    settings:
    lib.concatStrings (
      lib.mapAttrsToList (
        key: value:
        let
          rendered =
            if builtins.isList value then
              lib.concatStringsSep " " value
            else if builtins.isBool value then
              lib.boolToString value
            else
              toString value;
        in
        "${key} = ${rendered}\n"
      ) settings
    );

  # Each constructor below carries a default cap, because each one wraps a
  # known action whose work does not change with the project that calls it: a
  # checkout, an install of Nix, a read or a write of one artifact. Each
  # default is generous against the measured time, because the cap is there to
  # catch a step that stopped and not a step that is slow. A caller that knows
  # better passes `timeoutMinutes`.
  steps = {
    # `fetchDepth = 0` fetches the whole history. The default checkout is a
    # single commit, so a job that reads a range of commits needs this.
    checkout =
      {
        ref ? null,
        fetchDepth ? null,
        timeoutMinutes ? 10,
      }:
      {
        uses = "actions/checkout@main";
        timeout-minutes = timeoutMinutes;
      }
      // lib.optionalAttrs (ref != null || fetchDepth != null) {
        "with" =
          lib.optionalAttrs (ref != null) { inherit ref; }
          // lib.optionalAttrs (fetchDepth != null) { fetch-depth = fetchDepth; };
      };

    # `experimentalFeatures` names what the *installed* Nix has to allow.
    #
    # A job that makes its own daemon does not need this: `_start_daemon` in
    # `nanopynix_testing.nix_environment` pins the features on the command
    # line, on purpose, so that daemon does not read the host at all. A job
    # that runs against the daemon of the machine has no such seam, and the
    # installer writes `nix-command flakes` and nothing else.
    #
    # `settings` is everything else the installed Nix should hold, as
    # nix.conf keys. Substituters, public keys and `trusted-users` belong
    # there: a daemon ignores a substituter that a non-trusted user asks
    # for, so a project with a cache of its own needs both. A key named
    # there replaces the default for that key.
    #
    # `access-tokens` is a default because the limit it lifts is shared.
    # Anonymous api.github.com allows 60 calls an hour per IP, and GitHub's
    # runners share one NAT pool, so strangers spend that budget too. The
    # token makes it 1000 an hour per repository. Every `github:` input a
    # job resolves costs a call, and a fresh runner starts with an empty
    # tarball cache, so a wide matrix reaches 60 on its own. See nanopynix
    # issue #301.
    #
    # `github.token` is minted per job by GitHub and expires with the job.
    # It is not `secrets.GITHUB_TOKEN` wiring anyone configures, and a
    # fork's pull request gets one too.
    installNix =
      {
        timeoutMinutes ? 15,
        experimentalFeatures ? [
          "nix-command"
          "flakes"
        ],
        settings ? { },
        version ? null,
      }:
      {
        uses = "cachix/install-nix-action@master";
        timeout-minutes = timeoutMinutes;
        "with" = {
          extra_nix_config = nixConf (
            {
              experimental-features = experimentalFeatures;
              access-tokens = "github.com=\${{ github.token }}";
            }
            // settings
          );
        }
        // lib.optionalAttrs (version != null) {
          # The action takes an installer URL and not a version, and this is
          # the URL it fetches for its own default. A project names a version
          # when its tests assume one Nix on the machine -- the schema's
          # `nix.install.version` says what that cost when it was not named.
          install_url = "https://releases.nixos.org/nix/nix-${version}/install";
        };
      };

    cachix =
      {
        name ? "lillecarl",
        timeoutMinutes ? 15,
        useDaemon ? false,
        pushFilter ? null,
        skipPush ? null,
      }:
      {
        uses = "cachix/cachix-action@master";
        timeout-minutes = timeoutMinutes;
        "with" = {
          inherit name useDaemon;
          authToken = "\${{ secrets.CACHIX_AUTH_TOKEN }}";
        }
        // lib.optionalAttrs (pushFilter != null) { pushFilter = pushFilter; }
        // lib.optionalAttrs (skipPush != null) { skipPush = skipPush; };
      };

    /*
      Let the runner open an unprivileged user namespace.

      Ubuntu's AppArmor policy denies one by default, and two things here
      need it. The Nix sandbox is the obvious one. passt is the other: it
      isolates itself with `unshare(CLONE_NEWUSER)` before it serves a
      guest's uplink, so on a runner it exits at startup and every guest
      boots with no network -- reported, minutes later, as a name that
      would not resolve.

      The `unshare` proves the sysctls took, here rather than in the first
      test that needs them.

      A `run` body rather than an action, and this file used to hold a
      comment saying such a body did not belong here. It was right about
      the risk and wrong about the cost: the alternative was the same four
      lines written out in three repositories, which is how one of them
      came to be missing.
    */
    userNamespaces =
      {
        timeoutMinutes ? 5,
      }:
      {
        name = "Allow the unprivileged user namespaces Nix and passt need";
        timeout-minutes = timeoutMinutes;
        run = lib.concatStringsSep "; " [
          "sudo sysctl -w kernel.apparmor_restrict_unprivileged_userns=0"
          "sudo sysctl -w kernel.unprivileged_userns_clone=1"
          "unshare --user --map-root-user --mount --pid --fork --mount-proc true"
        ];
      };

    /*
      Let the runner user open /dev/kvm.

      The device is there on an x64 runner and the user is not in the `kvm`
      group, so without this QEMU exits with "Could not access KVM kernel
      module: Permission denied".

      x64 only. GitHub's ARM runners have no /dev/kvm at all, and a backend
      that asks for `accel=kvm` and never `accel=kvm:tcg` would otherwise
      fall back to emulation in silence -- a one-minute test becoming a
      timeout nobody can explain.
    */
    openKvm =
      {
        timeoutMinutes ? 5,
      }:
      {
        name = "Let the runner user open /dev/kvm";
        timeout-minutes = timeoutMinutes;
        run = lib.concatStringsSep "; " [
          "echo 'KERNEL==\"kvm\", GROUP=\"kvm\", MODE=\"0666\", OPTIONS+=\"static_node=kvm\"' | sudo tee /etc/udev/rules.d/99-kvm4all.rules"
          "sudo udevadm control --reload-rules"
          "sudo udevadm trigger --name-match=kvm"
          "ls -l /dev/kvm"
        ];
      };

    /*
      A runner starts with about 25 GiB free, and a large closure does not
      fit beside toolchains no job here uses.

      `docker system prune` as well: the image ships a populated docker
      store, and a job that never runs a container is still paying for it.

      `df` on both sides, because the number is the only thing that says
      whether this is still worth a step.

      A glob that matches nothing is passed to `rm` literally, and `-f`
      makes that silent -- which is what `julia*` relies on.

      **One line, and `;` between the commands.** A consumer may hold a
      generated workflow to the rule that a `run:` body is one line, because
      nothing shellchecks a body that lives in a Nix string -- nanopynix
      states it in `tests/meta/test_ci_step_policy.py`. The step that puts a
      body in a package cannot serve here: this one makes room *for* Nix, so
      it runs before Nix is installed. `;` and not `&&`, so a prune that
      fails still leaves the second `df` to report the number.
    */
    freeDiskSpace =
      {
        timeoutMinutes ? 10,
      }:
      {
        name = "Make room on the runner";
        timeout-minutes = timeoutMinutes;
        # **Linux only.** Every path below is a Linux path and `docker` is on
        # no macOS runner, so on one of those the step can only fail -- and it
        # does, at `docker: command not found`, because the shell runs with
        # `-e`. An expression belongs in `if:`; the rule about a `run:` body
        # does not reach here.
        "if" = "runner.os == 'Linux'";
        run = lib.concatStringsSep "; " [
          "df -h /"
          "sudo rm -rf ${lib.concatStringsSep " " removable}"
          "docker system prune --all --force"
          "docker builder prune --all --force"
          "df -h /"
        ];
      };

    verifyClosure =
      {
        name,
        timeoutMinutes ? 15,
      }:
      {
        inherit name;
        timeout-minutes = timeoutMinutes;
        run = ''nix store verify --recursive --no-trust "$(readlink -f result)"'';
      };

    uploadArtifact =
      {
        name ? null,
        artifactName,
        path,
        cond ? "\${{ !cancelled() }}",
        timeoutMinutes ? 15,
      }:
      withCond cond (
        lib.optionalAttrs (name != null) { inherit name; }
        // {
          uses = "actions/upload-artifact@main";
          timeout-minutes = timeoutMinutes;
          "with" = {
            name = artifactName;
            inherit path;
          };
        }
      );

    /*
      Publish a built documentation tree to GitHub Pages.

      Three steps, because the middle one is an action that reads a
      directory and a store path is not one it can read: everything under
      /nix/store is read-only, and `actions/upload-pages-artifact` needs to
      walk and tar a tree it can open. `--no-preserve=mode,ownership` is
      what makes the copy writable.

      Both repositories that publish docs had these three written out, with
      the actions pinned at different versions -- `@v3` and `@v4` in one,
      `@main` in the other. `@main` here, which is what every other action
      this file names uses.
    */
    preparePages =
      {
        path ? "result",
        timeoutMinutes ? 5,
      }:
      {
        name = "Prepare the Pages artifact";
        timeout-minutes = timeoutMinutes;
        run = lib.concatStringsSep "; " [
          "mkdir -p public"
          "cp -r --no-preserve=mode,ownership ${path}/. public/"
        ];
      };

    uploadPages =
      {
        timeoutMinutes ? 10,
      }:
      {
        uses = "actions/upload-pages-artifact@main";
        timeout-minutes = timeoutMinutes;
        "with".path = "public";
      };

    # Needs `permissions.pages = "write"` and `id-token = "write"` on the
    # job, and an `environment` naming github-pages. Those are job-level and
    # stay with the caller.
    deployPages =
      {
        id ? "deployment",
        timeoutMinutes ? 10,
      }:
      {
        inherit id;
        name = "Deploy to GitHub Pages";
        uses = "actions/deploy-pages@main";
        timeout-minutes = timeoutMinutes;
      };

    downloadArtifact =
      {
        artifactName,
        timeoutMinutes ? 10,
      }:
      {
        uses = "actions/download-artifact@main";
        timeout-minutes = timeoutMinutes;
        "with" = {
          name = artifactName;
        };
      };
  };
}

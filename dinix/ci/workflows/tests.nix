# The test workflow, as a value. nixidae renders it to
# `.github/workflows/dinix-tests.yml`, with dinix as the working directory of
# every step.
#
# One job that builds `checks`: every collection in every mode, both container
# tests, the Python packages and the docs. One job and not one per test,
# because the guest system and the UML kernel are built here, and a second job
# would build them again rather than share them.
{
  name = "tests";

  on = {
    push = null;
    pull_request = null;
  };

  jobs.checks = {
    runs-on = "ubuntu-24.04";
    # A cold runner builds the UML kernel, the guest system and every service
    # closure before the first guest boots. GitHub's own ceiling is 6 hours.
    timeout-minutes = 350;
    steps = [
      {
        uses = "actions/checkout@v4";
        timeout-minutes = 10;
      }
      # The images and closures under test do not fit beside the runner's
      # preinstalled toolchains.
      {
        name = "Make room on the runner";
        timeout-minutes = 10;
        run = "sudo rm -rf /usr/lib/jvm /usr/share/dotnet /usr/share/swift /usr/local/.ghcup /usr/local/julia* /usr/local/lib/android /opt/az /usr/local/share/powershell /usr/local/share/chromium /opt/microsoft /opt/google /usr/lib/firefox && docker system prune --all --force && df -h /";
      }
      {
        uses = "cachix/install-nix-action@master";
        timeout-minutes = 15;
        # A runner has four cores and a guest is a process, so running several
        # builds at once only makes each one slower.
        "with".extra_nix_config = ''
          access-tokens = github.com=''${{ github.token }}
          experimental-features = nix-command flakes
          trusted-users = root runner
          cores = 4
          max-jobs = 1
        '';
      }
      # A UML guest boots inside the Nix sandbox, which unshares a user
      # namespace, and Ubuntu's AppArmor denies an unprivileged one. The
      # unshare proves the sysctls took.
      {
        name = "Allow an unprivileged user namespace";
        timeout-minutes = 5;
        run = "sudo sysctl -w kernel.apparmor_restrict_unprivileged_userns=0; sudo sysctl -w kernel.unprivileged_userns_clone=1; unshare --user --map-root-user --mount --pid --fork --mount-proc true";
      }
      # Evaluation answers in about a minute what a typo in a collection would
      # otherwise cost the whole build to find.
      {
        name = "Evaluate every check";
        timeout-minutes = 20;
        run = "nix eval --raw --file .. dinix.checks.drvPath";
      }
      # --keep-going: one failing collection should not hide the state of the
      # others.
      {
        name = "Build every check";
        timeout-minutes = 330;
        run = "nix build --file .. dinix.checks --no-link --print-build-logs --keep-going";
      }
    ];
  };
}

# The test workflow, as a value. nixidae renders it to
# `.github/workflows/modiff-tests.yml`, with modiff as the working directory
# of every step. Tests run only in nixidae. nixidae/nixidae#4.
{
  name = "tests";

  on = {
    pull_request = null;
    push = null;
  };

  jobs.checks = {
    runs-on = "ubuntu-24.04";
    steps = [
      { uses = "actions/checkout@v4"; }
      {
        uses = "cachix/install-nix-action@master";
        "with".extra_nix_config = ''
          access-tokens = github.com=''${{ github.token }}
          experimental-features = nix-command flakes
          trusted-users = root runner
          trusted-public-keys = cache.nixos.org-1:6NCHdD59X431o0gWypbMrAURkbJ16ZPMQFGspcDShjY= lillecarl.cachix.org-1:NN/LLMg7mbyvZCu32Qlo8LpSHqNw7Rr3VBCEYQvRpT0=
          substituters = https://cache.nixos.org?priority=1 https://lillecarl.cachix.org?priority=2
        '';
      }
      {
        name = "Run the checks";
        run = "nix build --file . --no-link --print-build-logs checks.pytest checks.lint checks.format checks.types";
      }
    ];
  };
}

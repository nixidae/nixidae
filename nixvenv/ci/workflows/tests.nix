# The test workflow, as a value. nixidae renders it to
# `.github/workflows/nixvenv-tests.yml`, with nixvenv as the working
# directory of every step. nixidae/nixidae#4.
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
        '';
      }
      # nixvenv takes `pkgs` and pins none, so the umbrella gives it one.
      {
        name = "Build checks";
        run = "nix build --file .. nixvenv.checks --no-link --print-build-logs";
      }
    ];
  };
}

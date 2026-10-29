# The test workflow, as a value. nixidae renders it to
# `.github/workflows/huggorm-tests.yml`, with huggorm as the working
# directory of every step. nixidae/nixidae#4.
#
# One job per lane builds that lane's `suite`. Building the package does not
# run the suite (packages/huggorm/suite.nix says why), so this is the gate.
# `check` is the tree-wide lint, typecheck and clang -Werror gate: it broke
# unseen while nothing but a person ran it (nixidae/nixidae#1059).
{ lanes }:
let
  setup = [
    { uses = "actions/checkout@v4"; }
    {
      uses = "cachix/install-nix-action@master";
      "with".extra_nix_config = ''
        access-tokens = github.com=''${{ github.token }}
        experimental-features = nix-command flakes
      '';
    }
    {
      uses = "cachix/cachix-action@master";
      "with" = {
        authToken = "\${{ secrets.CACHIX_AUTH_TOKEN }}";
        name = "lillecarl";
        useDaemon = false;
      };
    }
  ];
in
{
  name = "tests";

  on = {
    pull_request = null;
    push = null;
  };

  jobs.suite = {
    runs-on = "ubuntu-24.04";
    timeout-minutes = 120;
    strategy = {
      fail-fast = false;
      matrix.lane = lanes;
    };
    env.LANE = "\${{ matrix.lane }}";
    steps = setup ++ [
      {
        name = "Build the lane's suite";
        run = ''nix build --file . "lanes.\"$LANE\".suite" --no-link --print-build-logs'';
      }
    ];
  };

  jobs.check = {
    runs-on = "ubuntu-24.04";
    timeout-minutes = 120;
    steps = setup ++ [
      {
        name = "Lint, typecheck, clang -Werror";
        run = "nix run --file . internal.check";
      }
    ];
  };
}

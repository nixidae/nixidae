# The CI workflow, as a value. `.github/workflows/ci.yml` is rendered from
# this file: edit this one, then run `nix run --file . ci-workflow-update`.
# `checks.ciWorkflow` fails when the two differ. pynixd's ci/workflows has
# the reasons for each choice below.
{ lib, ghalib, nixVersion }:
let
  mainOnly = "github.ref == 'refs/heads/main'";
  umbrellaRev = "\${{ needs.umbrella-rev.outputs.rev }}";

  bootstrap = {
    checkout.enable = true;
    nix.install = {
      enable = true;
      version = nixVersion;
      # `flakes` gives `builtins.fetchTree`, which nix/sources.nix fetches the
      # umbrella with.
      experimentalFeatures = [
        "nix-command"
        "flakes"
      ];
      settings = {
        trusted-users = [
          "root"
          "runner"
        ];
        substituters = [
          "https://cache.nixos.org?priority=1"
          "https://lillecarl.cachix.org?priority=2"
        ];
        trusted-public-keys = [
          "cache.nixos.org-1:6NCHdD59X431o0gWypbMrAURkbJ16ZPMQFGspcDShjY="
          "lillecarl.cachix.org-1:NN/LLMg7mbyvZCu32Qlo8LpSHqNw7Rr3VBCEYQvRpT0="
        ];
      };
    };
  };
in
ghalib.evalWorkflow {
  name = "CI";

  on = {
    push.branches = [ "**" ];
    pull_request = null;
  };

  # The umbrella fetches each source over git, not api.github.com, whose
  # anonymous limit the runners share (nanopynix#301).
  env.UMBRELLA_GIT = "1";

  jobs = {
    # The umbrella that locks this commit, for every job of the run.
    umbrella-rev = {
      runs-on = "ubuntu-24.04";
      timeout-minutes = 5;
      outputs.rev = "\${{ steps.resolve.outputs.rev }}";
      ghanix.checkout = {
        enable = true;
        fetchDepth = 100;
      };
      ghanix.freeDiskSpace.enable = false;
      steps = [
        {
          id = "resolve";
          name = "Resolve the umbrella revision";
          run = "ci/walkback.sh https://github.com/nixidae/nixidae pydd | sed 's/^/rev=/' >> \"$GITHUB_OUTPUT\"";
        }
      ];
    };

    # Sphinx renders docs/, and docs/_generated holds the measurements that
    # `nix run --file . measure-modes` wrote. A runner cannot run a
    # `builder-rpc-v0` planner in a private store, so the site never
    # measures.
    docs-build = {
      "if" = mainOnly;
      needs = "umbrella-rev";
      env.UMBRELLA_REV = umbrellaRev;
      runs-on = "ubuntu-24.04";
      timeout-minutes = 20;
      ghanix = bootstrap;
      steps = [
        {
          name = "Build documentation";
          run = "nix build --file . pydd-docs --out-link result --print-build-logs --print-out-paths";
        }
        {
          name = "Prepare Pages artifact";
          run = ''
            mkdir -p public
            cp -r --no-preserve=mode,ownership result/. public/
          '';
        }
        {
          uses = "actions/upload-pages-artifact@v3";
          "with".path = "public";
        }
      ];
    };

    docs-deploy = {
      "if" = mainOnly;
      needs = "docs-build";
      runs-on = "ubuntu-24.04";
      ghanix.freeDiskSpace.enable = false;
      permissions = {
        pages = "write";
        id-token = "write";
      };
      environment = {
        name = "github-pages";
        url = "\${{ steps.deployment.outputs.page_url }}";
      };
      concurrency = {
        group = "pages";
        cancel-in-progress = false;
      };
      steps = [
        {
          name = "Deploy to GitHub Pages";
          id = "deployment";
          uses = "actions/deploy-pages@v4";
        }
      ];
    };
  };
}

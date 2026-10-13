# Every check of the test projects, by name, in the shape that `run` builds:
# `out` is the check, `environment` is the environment it checks, and
# `closure` is what the planners run on.
#
#     nix run --file . run -- projects.cli.pure-app
#     nix run --file . test-projects
#
# The variants are the ones the ddrn lab's `test_projects.py` ran: one lock
# file, and the selections and forms that it does not record.
{ pkgs, pydd }:
let
  check = file: arguments: import file ({ inherit pkgs pydd; } // arguments);
  everyMember = [
    "ws-cli"
    "ws-core"
    "rich"
    "markdown-it-py"
    "mdurl"
    "pygments"
  ];
  checks = {
    pure-app = check ./check-uv.nix { };
    packaged-app = check ./check-packaged.nix { };
    packaged-sdist = check ./check-sdist.nix { };
    workspace-all = check ./check-workspace.nix {
      venvName = "workspace-all";
      expect = everyMember;
    };
    workspace-core = check ./check-workspace.nix {
      venvName = "workspace-core";
      select = {
        ws-core = [ ];
      };
      expect = [ "ws-core" ];
    };
    workspace-core-sdist = check ./check-workspace.nix {
      venvName = "workspace-core-sdist";
      expect = everyMember;
      fromSdist = [ "ws-core" ];
    };
    application = check ./check-application.nix { };
    packaged-app-bin = check ./check-application.nix {
      project = ./packaged-app;
      editableTree = ./packaged-app;
      application = [ "packaged-app" ];
      venvName = "packaged-app-bin";
    };
    binary-app = check ./check-binary.nix { };
    binary-nixpkgs = check ./check-binary.nix {
      venvName = "binary-nixpkgs";
      fromNixpkgs.pyzmq = pkgs.python3Packages.pyzmq;
    };
    native-app = check ./check-native.nix { };
    git-app = check ./check-git.nix { };
    vendored-app = check ./check-vendored.nix { };
    local-index = check ./check-index.nix { };
  };
  # Every planner here runs on the same interpreter and the same `nix`.
  inherit (pydd.python.uv { project = ./pure-app; }) closure;
in
builtins.mapAttrs (_: out: {
  inherit out closure;
  environment = out.planner.out;
}) checks

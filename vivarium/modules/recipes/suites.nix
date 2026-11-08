# pytest suites that run inside a guest, one phase each.
#
# A suite run from the host would read the host's configuration and
# share its stores. Run inside a guest, it gets a machine of its own.
# Each suite's JUnit becomes cases of the run, and the driver's
# `-- <args>` reach every suite's pytest.
{ config, lib, ... }:
let
  cfg = config.vivarium.recipes.suites;
  inherit (lib) mkOption types;
  inStore = lib.filter (lib.hasPrefix builtins.storeDir);
in
{
  options.vivarium.recipes.suites = mkOption {
    default = { };
    example = lib.literalExpression ''
      {
        unit = { tests = "tests/unit"; directory = "/work"; after = [ "prepare" ]; };
      }
    '';
    description = ''
      pytest suites to run inside a guest, by phase name. Each one is a
      phase of that name on one guest. Suites on different guests run at
      the same time.
    '';
    type = types.attrsOf (
      types.submodule (
        { name, ... }:
        {
          options = {
            node = mkOption {
              type = types.str;
              default = name;
              description = "The guest the suite runs in.";
            };
            tests = mkOption {
              type = types.str;
              description = "What pytest collects, relative to `directory`.";
            };
            directory = mkOption {
              type = types.str;
              default = "/";
              description = ''
                Where pytest runs. A store path is added to the guest's
                `vivarium.lateClosure`.
              '';
            };
            args = mkOption {
              type = types.listOf types.str;
              default = [ ];
              example = [ "-q" ];
              description = "Given to pytest before the tests.";
            };
            pytest = mkOption {
              type = types.str;
              default = "pytest";
              example = lib.literalExpression ''"''${pkgs.python3.withPackages (p: [ p.pytest ])}/bin/pytest"'';
              description = ''
                The pytest to run. A store path is added to the guest's
                `vivarium.lateClosure`; a bare name is looked up on the
                user's PATH.
              '';
            };
            user = mkOption {
              type = types.str;
              default = "root";
              description = "Who runs the suite, through a login shell.";
            };
            environment = mkOption {
              type = types.attrsOf types.str;
              default = { };
              example = {
                NIX_BIN = "$(command -v nix)";
              };
              description = ''
                Variables for pytest. Each value is a shell word, so
                `$(...)` expands in the guest.
              '';
            };
            delegateCgroup = mkOption {
              type = types.bool;
              default = false;
              description = ''
                Run the suite in a cgroup its user owns, as `Delegate=yes`
                gives a unit. For a suite whose code makes cgroups.
              '';
            };
            after = mkOption {
              type = types.listOf types.str;
              default = [ "boot" ];
              description = "The phase's `after`.";
            };
            timeout = mkOption {
              type = types.ints.positive;
              default = 3600;
              description = "Seconds the suite may run; the phase's `timeout`.";
            };
          };
        }
      )
    );
  };

  config = lib.mkIf (cfg != { }) {
    settings.guestSuites = lib.mapAttrs (_: suite: {
      inherit (suite)
        node
        tests
        directory
        args
        pytest
        user
        environment
        delegateCgroup
        timeout
        ;
    }) cfg;

    phases = lib.mapAttrs (_: suite: {
      script = ../../recipes/suite.py;
      inherit (suite) after timeout;
      nodes = [ suite.node ];
      description = lib.mkDefault "pytest ${suite.tests} in ${suite.node}";
    }) cfg;

    defaults =
      { config, ... }:
      {
        vivarium.lateClosure = lib.concatMap (
          suite:
          inStore [
            suite.directory
            suite.pytest
          ]
        ) (lib.filter (suite: suite.node == config.networking.hostName) (lib.attrValues cfg));
      };
  };
}

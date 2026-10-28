# The test workflows of every source, placed for this repository.
#
# A source exposes `ciTestWorkflows`, an attrset of workflow values written
# for its own directory. Each one renders to
# `.github/workflows/<source>-<name>.yml`, with the source as the working
# directory of every `run:` step. An action does not read that default, so a
# relative `path` or `files` it takes gets the source's directory in front.
#
# Tests run only here. A source's own repository runs its Pages workflow.
# nixidae/nixidae#4.
{
  lib,
  pkgs,
  ghanix,
  nixvenv,
  sets,
}:
let
  relative = p: p != "" && !(lib.hasPrefix "/" p) && !(lib.hasPrefix "~" p) && !(lib.hasPrefix "$" p);

  placePaths =
    source: value:
    lib.concatMapStringsSep "\n" (line: if relative line then "${source}/${line}" else line) (
      lib.splitString "\n" value
    );

  placeStep =
    source: step:
    if step ? uses && step ? "with" then
      step
      // {
        "with" = lib.mapAttrs (
          key: value:
          if
            builtins.elem key [
              "path"
              "files"
            ]
            && builtins.isString value
          then
            placePaths source value
          else
            value
        ) step."with";
      }
    else
      step;

  place =
    source: workflow:
    workflow
    // {
      name = "${source}: ${workflow.name or "CI"}";
      defaults = lib.recursiveUpdate (workflow.defaults or { }) { run.working-directory = source; };
      jobs = lib.mapAttrs (
        _: job: job // lib.optionalAttrs (job ? steps) { steps = map (placeStep source) job.steps; }
      ) workflow.jobs;
    };

  workflows = lib.concatMapAttrs (
    source: set:
    lib.mapAttrs' (
      name: workflow: lib.nameValuePair "${source}-${name}" (place source workflow)
    ) set.ciTestWorkflows
  ) sets;

  files = lib.mapAttrs (
    name: workflow:
    pkgs.runCommand "${name}.yml"
      {
        nativeBuildInputs = [
          (nixvenv {
            python = pkgs.python3;
            packages = ps: [ ps.pyyaml ];
            executables = [ "python3" ];
          })
        ];
        value = builtins.toJSON workflow;
        passAsFile = [ "value" ];
      }
      ''
        {
          echo "# GENERATED FILE -- do not edit by hand."
          echo "# Edit the source's ciTestWorkflows, then run: nix run --file . ci-workflow-update"
          python3 ${ghanix.lib.toYamlScript} "$valuePath"
        } > $out
      ''
  ) workflows;

  sources = lib.attrNames sets;
in
{
  inherit workflows files;

  # Writes every rendered file, and removes a `<source>-*.yml` that no
  # workflow renders to any more.
  update = pkgs.writeShellScriptBin "ci-workflow-update" ''
    set -euo pipefail
    # A secondary jj workspace has no .git. nixidae/nixidae#18.
    if ! root=$(${lib.getExe pkgs.jujutsu} root 2>/dev/null) &&
      ! root=$(${lib.getExe pkgs.git} rev-parse --show-toplevel 2>/dev/null); then
      echo "ci-workflow-update: $PWD is in no jj workspace and no git checkout" >&2
      exit 1
    fi
    cd "$root/.github/workflows"
    for source in ${lib.escapeShellArgs sources}; do
      rm -f "$source"-*.yml
    done
    ${lib.concatStrings (
      lib.mapAttrsToList (name: file: "cp --no-preserve=mode ${file} ${name}.yml\n") files
    )}
  '';

  # Does every committed file still say what its source says? Parsed, so key
  # order and quoting cannot fail it. yq reads YAML 1.2, where `on:` is a
  # string key.
  check =
    pkgs.runCommand "ci-workflows-in-step"
      {
        nativeBuildInputs = [
          pkgs.yq-go
          pkgs.jq
        ];
        committed = ../.github/workflows;
      }
      (
        ''
          for source in ${lib.escapeShellArgs sources}; do
            for file in "$committed/$source"-*.yml; do
              [ -e "$file" ] || continue
              case " ${toString (lib.attrNames files)} " in
                *" $(basename "$file" .yml) "*) ;;
                *) echo "$file renders from nothing. Run: nix run --file . ci-workflow-update" >&2; exit 1 ;;
              esac
            done
          done
        ''
        + lib.concatStrings (
          lib.mapAttrsToList (name: workflow: ''
            if [ ! -e "$committed/${name}.yml" ]; then
              echo ".github/workflows/${name}.yml is missing. Run: nix run --file . ci-workflow-update" >&2
              exit 1
            fi
            yq --output-format=json '.' "$committed/${name}.yml" | jq --sort-keys . > committed.json
            jq --sort-keys . < ${pkgs.writeText "${name}.json" (builtins.toJSON workflow)} > rendered.json
            if ! diff --unified committed.json rendered.json; then
              echo ".github/workflows/${name}.yml disagrees with its source. Run: nix run --file . ci-workflow-update" >&2
              exit 1
            fi
          '') workflows
        )
        + "touch $out\n"
      );
}
